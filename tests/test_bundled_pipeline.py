"""The bundled examples run end to end from the repository as checked in.

These tests exist so the pipeline cannot rot into something that only works on
the author's machine: they execute compile, approve, export, run, and report
against the committed contracts, fixtures, and price table, for both domains.
"""

import json

import pytest
from typer.testing import CliRunner

from discoveryspec.cli import app
from tests.conftest import (
    APPROVED_PATH,
    EXAMPLE,
    REFUND,
    REFUND_APPROVED_PATH,
    REPO,
)

runner = CliRunner()

pytest.importorskip(
    "agent_eval_gate", reason="agent-eval-gate not installed; run-pipeline tests skipped"
)

PRICES = REPO / "examples" / "prices.json"


def combined(result) -> str:
    return result.output + (result.stderr or "")


def run_suite(tmp_path, example_dir, contract_path):
    out = tmp_path / "gate-run"
    result = runner.invoke(app, [
        "run", "--contract", str(contract_path),
        "--mode", "replay", "--fixtures", str(example_dir / "fixtures"),
        "--prices", str(PRICES), "--out", str(out),
    ])
    return result, out


@pytest.mark.parametrize(
    "example_dir,contract_path,unit",
    [
        (EXAMPLE, APPROVED_PATH, "invoice"),
        (REFUND, REFUND_APPROVED_PATH, "refund request"),
    ],
    ids=["invoice_automation", "refund_handling"],
)
def test_bundled_example_runs_clean(tmp_path, example_dir, contract_path, unit):
    result, out = run_suite(tmp_path, example_dir, contract_path)
    assert result.exit_code == 0, combined(result)

    report = json.loads((out / "run-report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert report["passed"] == report["total"] == 10
    assert report["slo"]["p95_latency_ms"]["passed"]
    assert report["slo"]["cost_per_task_eur"]["passed"]
    assert report["slo"]["cost_per_task_eur"]["unit"] == unit
    # every verdict carries the promise behind it
    for entry in report["scenarios"]:
        assert entry["enforces"]["statement"].strip()
        assert entry["citations"], entry["scenario_id"]


@pytest.mark.parametrize(
    "example_dir,contract_path,brand",
    [
        (EXAMPLE, APPROVED_PATH, "nordlicht-strict.json"),
        (REFUND, REFUND_APPROVED_PATH, "neutral.json"),
    ],
    ids=["invoice_automation", "refund_handling"],
)
def test_bundled_example_renders_a_report(tmp_path, example_dir, contract_path, brand):
    result, out = run_suite(tmp_path, example_dir, contract_path)
    assert result.exit_code == 0, combined(result)

    html_path = tmp_path / "report.html"
    rendered = runner.invoke(app, [
        "report", "--contract", str(contract_path), "--run", str(out),
        "--brand", str(REPO / "examples" / "brands" / brand),
        "--out", str(html_path),
    ])
    assert rendered.exit_code == 0, combined(rendered)
    html = html_path.read_text(encoding="utf-8")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert "Cleared to deploy" in html
    # the page names the system under test, never a domain baked into the code
    assert contract["metadata"]["system"] in html
    # the cost ceiling is labelled with this contract's own unit of work
    assert f"Cost per processed {contract['slo']['cost_per_task_eur']['unit']}" in html

    # and the section a reader needs most: not just its heading, but every
    # promise it excuses, with the reason and the named verifier
    assert "What this suite does not check" in html
    excused = [
        r for r in contract["requirements"] if r.get("out_of_band_verification")
    ]
    assert excused, "the bundled example should exercise this section"
    for req in excused:
        assert req["title"] in html
        assert req["stakeholder"] in html
        assert req["out_of_band_verification"]["reason"] in html
        assert req["out_of_band_verification"]["verified_by"] in html


def test_run_console_names_what_it_did_not_check(tmp_path):
    # the operator reading stdout must not have to open a JSON file to learn
    # that a promise went untested
    result, _ = run_suite(tmp_path, EXAMPLE, APPROVED_PATH)
    assert result.exit_code == 0, combined(result)
    output = combined(result)
    assert "not covered by this suite: REQ-011" in output
    assert "not covered by this suite: REQ-012" in output
    assert "Nordlicht IT infrastructure review before go-live" in output


def test_report_refuses_a_run_report_with_an_unreadable_slo_block(tmp_path):
    """A foreign or hand-edited run report must fail closed, not traceback.

    Exit 2 is the documented code for an integrity failure; a KeyError escaping
    to the console would be exit 1, which is the code for a failed acceptance.
    """
    _, out = run_suite(tmp_path, EXAMPLE, APPROVED_PATH)
    report_path = out / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["slo"].pop("cost_per_task_eur")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rendered = runner.invoke(app, [
        "report", "--contract", str(APPROVED_PATH), "--run", str(out),
        "--out", str(tmp_path / "report.html"),
    ])
    assert rendered.exit_code == 2, combined(rendered)
    assert "Traceback" not in combined(rendered)
    assert "cost_per_task_eur" in combined(rendered)
    assert not (tmp_path / "report.html").exists()


def test_cost_ceiling_is_not_passed_by_a_hair(tmp_path):
    """The bundled run should have real headroom against the ceiling.

    A fixture set tuned to pass by a rounding error would make the cost SLO look
    enforced while proving nothing, so this asserts the margin is meaningful.
    """
    _, out = run_suite(tmp_path, EXAMPLE, APPROVED_PATH)
    cost = json.loads((out / "run-report.json").read_text(encoding="utf-8"))["slo"][
        "cost_per_task_eur"
    ]
    assert cost["max_observed"] < cost["limit"] * 0.8, cost


def test_signed_pipeline_end_to_end(tmp_path):
    """keygen, sign the approval, run signed, verify both, refuse a swap."""
    import shutil

    assert runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")]).exit_code == 0

    # the bundled approved contract is signed by nobody, so re-approve it here
    # under the fresh key: that is the whole point of the signed workflow
    resolved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
    resolved["metadata"].update(
        status="draft", approved_by=None, approved_at=None, transcript_sha256=None
    )
    (tmp_path / "resolved.json").write_text(
        json.dumps(resolved, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copyfile(EXAMPLE / "transcript.md", tmp_path / "transcript.md")

    signed_contract = tmp_path / "approved.json"
    approved = runner.invoke(app, [
        "approve", "--contract", str(tmp_path / "resolved.json"),
        "--by", "Anna Lindqvist (Operations)", "--date", "2026-07-16",
        "--out", str(signed_contract), "--signing-key", str(tmp_path / "key.pem"),
    ])
    assert approved.exit_code == 0, combined(approved)

    out = tmp_path / "gate-run"
    signed_run = runner.invoke(app, [
        "run", "--contract", str(signed_contract),
        "--mode", "replay", "--fixtures", str(EXAMPLE / "fixtures"),
        "--prices", str(PRICES), "--out", str(out),
        "--verify-key", str(tmp_path / "key.pub"),
        "--signing-key", str(tmp_path / "key.pem"),
    ])
    assert signed_run.exit_code == 0, combined(signed_run)
    assert "signature: VERIFIED" in signed_run.output
    assert "run report attestation: Ed25519-signed" in signed_run.output

    ok = runner.invoke(app, [
        "report", "--contract", str(signed_contract), "--run", str(out),
        "--out", str(tmp_path / "report.html"),
        "--verify-key", str(tmp_path / "key.pub"),
    ])
    assert ok.exit_code == 0, combined(ok)
    assert "run report attestation: VERIFIED" in ok.output

    # the other domain's contract must not render against this run
    swapped = runner.invoke(app, [
        "report", "--contract", str(REFUND_APPROVED_PATH), "--run", str(out),
        "--out", str(tmp_path / "swapped.html"),
    ])
    assert swapped.exit_code == 2
    assert "do not belong together" in combined(swapped)
