"""CLI attestation policy: --signing-key on approve/run, --verify-key on
validate/export-gate/run/report, and the forgery refusals under a key."""

import hashlib
import json
import shutil

import pytest
from typer.testing import CliRunner

from discoveryspec import generate_keypair
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, TRANSCRIPT_PATH

runner = CliRunner()


def combined(result) -> str:
    return result.output + (result.stderr or "")


@pytest.fixture()
def workspace(tmp_path):
    private_pem, public_pem = generate_keypair()
    (tmp_path / "key.pem").write_bytes(private_pem)
    (tmp_path / "pub.pem").write_bytes(public_pem)
    other_priv, other_pub = generate_keypair()
    (tmp_path / "other-pub.pem").write_bytes(other_pub)
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    approved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
    resolved = dict(approved)
    resolved["metadata"] = dict(approved["metadata"])
    resolved["metadata"].update(
        status="draft", approved_by=None, approved_at=None, transcript_sha256=None
    )
    (tmp_path / "resolved.json").write_text(
        json.dumps(resolved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return tmp_path


def approve(ws, out="approved.json", sign=True):
    args = ["approve", "--contract", str(ws / "resolved.json"),
            "--by", "Anna, Jonas, Priya", "--date", "2026-07-16",
            "--out", str(ws / out)]
    if sign:
        args += ["--signing-key", str(ws / "key.pem")]
    return runner.invoke(app, args)


def test_approve_signs_and_validate_verifies(workspace):
    ws = workspace
    result = approve(ws)
    assert result.exit_code == 0, combined(result)
    assert "Ed25519-signed" in result.output
    signed = json.loads((ws / "approved.json").read_text(encoding="utf-8"))
    assert signed["metadata"]["approval_signature"]["algorithm"] == "ed25519"

    check = runner.invoke(app, [
        "validate", "--contract", str(ws / "approved.json"),
        "--require-approved", "--verify-key", str(ws / "pub.pem"),
    ])
    assert check.exit_code == 0, combined(check)
    assert "VERIFIED" in check.output


def test_edited_approver_fails_verification(workspace):
    ws = workspace
    assert approve(ws).exit_code == 0
    forged = json.loads((ws / "approved.json").read_text(encoding="utf-8"))
    forged["metadata"]["approved_by"] = "Mallory"
    (ws / "forged.json").write_text(json.dumps(forged, indent=2), encoding="utf-8")
    result = runner.invoke(app, [
        "validate", "--contract", str(ws / "forged.json"),
        "--require-approved", "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 2
    assert "modified after signing" in combined(result)


def test_hand_crafted_unsigned_approval_fails_under_key(workspace):
    ws = workspace
    resolved = json.loads((ws / "resolved.json").read_text(encoding="utf-8"))
    resolved["metadata"].update(
        status="approved", approved_by="Mallory", approved_at="2026-07-17",
        transcript_sha256=hashlib.sha256(
            (ws / "transcript.md").read_bytes()).hexdigest(),
    )
    (ws / "forge.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")
    result = runner.invoke(app, [
        "validate", "--contract", str(ws / "forge.json"),
        "--require-approved", "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 2
    assert "no attestation" in combined(result)


def test_wrong_key_is_rejected(workspace):
    ws = workspace
    assert approve(ws).exit_code == 0
    result = runner.invoke(app, [
        "validate", "--contract", str(ws / "approved.json"),
        "--require-approved", "--verify-key", str(ws / "other-pub.pem"),
    ])
    assert result.exit_code == 2
    assert "different key" in combined(result)


def test_unsigned_approval_without_key_still_validates(workspace):
    # backward compatibility: no key means structural checks only, and the
    # output names the gap
    ws = workspace
    assert approve(ws, sign=False).exit_code == 0
    result = runner.invoke(app, [
        "validate", "--contract", str(ws / "approved.json"), "--require-approved",
    ])
    assert result.exit_code == 0, combined(result)
    assert "not verified" in result.output


def test_draft_must_not_carry_a_signature(workspace):
    ws = workspace
    # sign a draft by hand-injecting a signature field, then verify a draft;
    # the block must be schema-shaped so the check under test (a draft
    # carrying ANY signature) is what fires, not the schema shape check
    draft = json.loads((ws / "resolved.json").read_text(encoding="utf-8"))
    draft["metadata"]["approval_signature"] = {
        "version": "attest.v1",
        "kind": "deployment-contract",
        "algorithm": "ed25519",
        "public_key_fingerprint": "0" * 16,
        "digest_sha256": "0" * 64,
        "signature": "aW52YWxpZA==",
    }
    (ws / "signed-draft.json").write_text(json.dumps(draft, indent=2), encoding="utf-8")
    result = runner.invoke(app, [
        "validate", "--contract", str(ws / "signed-draft.json"),
        "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 2
    assert "draft must not be signed" in combined(result)


def _prepare_run(ws, verdict_fail=False):
    """Approve+sign, export, build fixtures, run+sign; return the run dir.

    The skip guard lives here rather than in each caller: a caller that forgets
    it turns a missing optional dependency into a hard failure, which is what
    made the CI job without agent-eval-gate installed go red.
    """
    gate = pytest.importorskip(
        "agent_eval_gate", reason="agent-eval-gate not installed; run-command tests skipped"
    )

    assert approve(ws).exit_code == 0
    export = ws / "export"
    assert runner.invoke(app, [
        "export-gate", "--contract", str(ws / "approved.json"), "--out", str(export),
    ]).exit_code == 0
    scenarios = gate.load_scenarios(export / "scenarios.yaml")
    fixtures = ws / "fixtures"
    fixtures.mkdir()
    for scenario in scenarios:
        calls = [gate.ToolCallRecord(name=e.name, args=dict(e.args_subset))
                 for e in scenario.checks.expected_tools]
        text = " ".join(scenario.checks.output_must_contain) or "handled"
        if verdict_fail and scenario.id == "no-autonomous-posting-under-threshold":
            step = gate.StepRecord(
                index=0, stop_reason="end_turn", text="posted it",
                tool_calls=[gate.ToolCallRecord(
                    name="post_invoice_to_erp", args={"invoice_id": "INV-2044"})],
                usage=gate.Usage(input_tokens=800, output_tokens=200), latency_s=0.4)
        else:
            step = gate.StepRecord(
                index=0, stop_reason="end_turn", text=text, tool_calls=calls,
                usage=gate.Usage(input_tokens=800, output_tokens=200), latency_s=0.4)
        (fixtures / f"{scenario.id}.json").write_text(
            gate.Trajectory(scenario_id=scenario.id, model="claude-opus-4-8",
                            steps=[step], final_text=step.text).model_dump_json(indent=2),
            encoding="utf-8")
    (ws / "prices.json").write_text(
        json.dumps({"claude-opus-4-8": {"input_per_mtok": 4.6, "output_per_mtok": 23.0}}),
        encoding="utf-8")
    run_out = ws / "gate-run"
    runner.invoke(app, [
        "run", "--contract", str(ws / "approved.json"),
        "--fixtures", str(fixtures), "--prices", str(ws / "prices.json"),
        "--out", str(run_out), "--signing-key", str(ws / "key.pem"),
    ])
    return run_out


def test_run_signs_report_and_report_verifies(workspace):
    pytest.importorskip("agent_eval_gate")
    ws = workspace
    run_out = _prepare_run(ws)
    report_json = json.loads((run_out / "run-report.json").read_text(encoding="utf-8"))
    assert report_json["attestation"]["kind"] == "run-report"
    assert report_json["provenance"]["price_table_sha256"]

    result = runner.invoke(app, [
        "report", "--contract", str(ws / "approved.json"), "--run", str(run_out),
        "--out", str(ws / "report.html"), "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 0, combined(result)
    assert "VERIFIED" in result.output


def test_flipped_run_verdict_fails_report_under_key(workspace):
    pytest.importorskip("agent_eval_gate")
    ws = workspace
    run_out = _prepare_run(ws, verdict_fail=True)
    report_json = json.loads((run_out / "run-report.json").read_text(encoding="utf-8"))
    assert report_json["verdict"] == "FAIL"
    report_json["verdict"] = "PASS"
    for entry in report_json["scenarios"]:
        entry["passed"] = True
        entry["failed_checks"] = []
    (run_out / "run-report.json").write_text(
        json.dumps(report_json, indent=2), encoding="utf-8")

    result = runner.invoke(app, [
        "report", "--contract", str(ws / "approved.json"), "--run", str(run_out),
        "--out", str(ws / "forged.html"), "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 2
    assert "modified after signing" in combined(result)
    assert not (ws / "forged.html").exists()


def test_report_refuses_an_edited_run_json_next_to_a_signed_summary(workspace):
    # the signed run-report.json pins run.json's sha256; swapping the raw
    # gate evidence while keeping the authentic summary must refuse
    ws = workspace
    run_out = _prepare_run(ws)
    run_json = run_out / "run.json"
    raw = json.loads(run_json.read_text(encoding="utf-8"))
    raw["run_id"] = "swapped-in-evidence"
    run_json.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    result = runner.invoke(app, [
        "report", "--contract", str(ws / "approved.json"), "--run", str(run_out),
        "--out", str(ws / "report.html"), "--verify-key", str(ws / "pub.pem"),
    ])
    assert result.exit_code == 2
    assert "does not match the hash pinned" in combined(result)
