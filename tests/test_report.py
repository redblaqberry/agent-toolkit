"""Manager-report behavior: input binding, honest verdicts, closed styling,
brand rules as hard constraints, and safe rendering of untrusted text."""

import copy
import hashlib
import json

import pytest

from discoveryspec import (
    DEFAULT_BRAND,
    Brand,
    BrandError,
    ReportError,
    load_brand,
    load_contract,
    render_report,
)
from discoveryspec.brand import validate_brand
from discoveryspec.report import audit_stylesheet, brand_stylesheet, build_view
from tests.conftest import APPROVED_PATH, REPO

BRANDS = REPO / "examples" / "brands"

CONTRACT = load_contract(APPROVED_PATH)
CONTRACT_SHA = hashlib.sha256(APPROVED_PATH.read_bytes()).hexdigest()


def entry(scenario_id, label, passed=True, error=None, failed_checks=None):
    return {
        "scenario_id": scenario_id,
        "manager_label": label,
        "passed": passed,
        "score": 1.0 if passed else 0.3,
        "enforces": {
            "requirement_id": "REQ-005",
            "title": "Named human approval for every ERP write",
            "statement": "No automated system writes to the ERP without a named human approving that specific transaction.",
            "stakeholder": "Jonas Weber (Security)",
        },
        "cited_requirements": ["REQ-005"],
        "source_turns": [15],
        "citations": [
            {"turn": 15, "speaker": "Jonas Weber", "role": "Security",
             "text": "No automated system writes to the ERP without a named "
                     "human approving that specific transaction."},
        ],
        "failed_checks": failed_checks or [],
        "failed_criteria": [],
        "error": error,
        "latency_ms": 400.0,
        "cost_eur": 0.02,
    }


def make_run_report(verdict="PASS", entries=None):
    entries = entries if entries is not None else [
        entry("s1", "Processes a normal invoice, then stops to ask for approval"),
        entry("s2", "Refuses to post a small invoice without approval"),
    ]
    passed = sum(1 for e in entries if e["passed"])
    return {
        "source": {
            "project": CONTRACT.metadata.project,
            "customer": CONTRACT.metadata.customer,
            "transcript": CONTRACT.metadata.transcript,
            "transcript_sha256": CONTRACT.metadata.transcript_sha256,
            "approved_by": CONTRACT.metadata.approved_by,
            "approved_at": CONTRACT.metadata.approved_at,
            "contract_sha256": CONTRACT_SHA,
        },
        "run_id": "20260717-120000",
        "generated_at": "2026-07-17T12:00:00Z",
        "mode": "replay",
        "agent_model": "claude-test",
        "scenarios": entries,
        "passed": passed,
        "total": len(entries),
        "slo": {
            "p95_latency_ms": {"limit": 2000, "observed": 400.0, "passed": True},
            "cost_per_task_eur": {"limit": 0.08, "unit": "invoice", "max_observed": 0.02, "passed": True},
        },
        "verdict": verdict,
    }


# --- rendering ------------------------------------------------------------------

def test_pass_report_renders_plain_language():
    html = render_report(CONTRACT, make_run_report(), DEFAULT_BRAND, CONTRACT_SHA)
    assert "Cleared to deploy" in html
    assert CONTRACT.metadata.customer in html
    assert "Processes a normal invoice, then stops to ask for approval" in html
    assert "box-shadow" not in html
    assert "gradient(" not in html


def test_fail_report_quotes_the_promise():
    entries = [
        entry("s1", "Processes a normal invoice, then stops to ask for approval"),
        entry("s2", "Refuses to post a small invoice without approval",
              passed=False,
              failed_checks=[{"name": "forbidden_tool:post_invoice_to_erp",
                              "detail": "called 1x"}]),
    ]
    html = render_report(
        CONTRACT, make_run_report("FAIL", entries), DEFAULT_BRAND, CONTRACT_SHA
    )
    assert "Do not deploy" in html
    assert "No automated system writes to the ERP" in html
    assert "Jonas Weber, Security" in html
    assert "forbidden_tool:post_invoice_to_erp" in html


def test_incomplete_report_is_not_a_fail():
    entries = [
        entry("s1", "Processes a normal invoice, then stops to ask for approval"),
        entry("s2", "Blocks payment when a vendor's bank details change",
              passed=False, error="fixture not found"),
    ]
    html = render_report(
        CONTRACT, make_run_report("INCOMPLETE", entries), DEFAULT_BRAND, CONTRACT_SHA
    )
    assert "Run incomplete" in html
    assert "proves nothing" in html
    assert "NOT RUN" in html
    assert "Do not deploy" not in html


def test_untrusted_text_is_escaped():
    # citations render for failed scenarios, so the hostile text goes there
    entries = [entry("s1", "label", passed=False,
                     failed_checks=[{"name": "check", "detail": "detail"}])]
    entries[0]["citations"][0]["text"] = "<script>alert(1)</script> quoted"
    html = render_report(
        CONTRACT, make_run_report("FAIL", entries), DEFAULT_BRAND, CONTRACT_SHA
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_judge_outage_renders_as_not_run_not_broken_promise():
    entries = [
        entry("s1", "Processes a normal invoice, then stops to ask for approval"),
        entry("s2", "Posts only after a named approver releases it", passed=False),
    ]
    entries[1]["judge_error"] = True
    entries[1]["failed_criteria"] = [
        {"criterion": "judge criterion", "reasoning": "judge call failed: outage"},
    ]
    html = render_report(
        CONTRACT, make_run_report("INCOMPLETE", entries), DEFAULT_BRAND, CONTRACT_SHA
    )
    assert "NOT RUN" in html
    assert "judge was unavailable" in html
    assert "broken promise" not in html.lower()


def test_brand_identity_obeys_the_emoji_rule():
    import copy as copy_module

    branded = copy_module.deepcopy(DEFAULT_BRAND)
    branded.identity.company = "Acme 🚀 Group"
    with pytest.raises(ReportError) as excinfo:
        render_report(CONTRACT, make_run_report(), branded, CONTRACT_SHA)
    assert any("emoji" in p for p in excinfo.value.problems)


# --- input binding --------------------------------------------------------------

def test_report_refuses_wrong_contract():
    with pytest.raises(ReportError) as excinfo:
        build_view(CONTRACT, make_run_report(), "0" * 64)
    assert any("not the contract this run executed" in p for p in excinfo.value.problems)


def test_report_refuses_unbound_run():
    run_report = make_run_report()
    del run_report["source"]["contract_sha256"]
    with pytest.raises(ReportError) as excinfo:
        build_view(CONTRACT, run_report, CONTRACT_SHA)
    assert any("no contract_sha256 binding" in p for p in excinfo.value.problems)


def test_report_refuses_project_mismatch():
    run_report = make_run_report()
    run_report["source"]["project"] = "some-other-project"
    with pytest.raises(ReportError):
        build_view(CONTRACT, run_report, CONTRACT_SHA)


def test_report_refuses_missing_keys():
    with pytest.raises(ReportError):
        build_view(CONTRACT, {"source": {}}, CONTRACT_SHA)


# --- brand rules as hard constraints -------------------------------------------

def test_example_brands_load_and_validate():
    for name in ("neutral.json", "nordlicht-strict.json"):
        brand = load_brand(BRANDS / name)
        validate_brand(brand)


def test_brand_low_contrast_refused():
    raw = json.loads((BRANDS / "neutral.json").read_text(encoding="utf-8"))
    raw["palette"]["muted_ink"] = "#eeeeee"  # nearly invisible on paper
    brand = Brand.model_validate(raw)
    with pytest.raises(BrandError) as excinfo:
        validate_brand(brand)
    assert any("muted_ink on paper" in p for p in excinfo.value.problems)


def test_brand_unknown_key_refused(tmp_path):
    raw = json.loads((BRANDS / "neutral.json").read_text(encoding="utf-8"))
    raw["custom_css"] = "body { box-shadow: 0 0 9px red }"
    bad = tmp_path / "brand.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BrandError) as excinfo:
        load_brand(bad)
    assert any("custom_css" in p for p in excinfo.value.problems)


def test_brand_malformed_color_refused(tmp_path):
    raw = json.loads((BRANDS / "neutral.json").read_text(encoding="utf-8"))
    raw["palette"]["accent"] = "cornflowerblue; } body { background: url(evil)"
    bad = tmp_path / "brand.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BrandError):
        load_brand(bad)


def test_no_shadow_rule_catches_violations():
    brand = copy.deepcopy(DEFAULT_BRAND)
    rules = brand_stylesheet(brand) + [
        (".sneaky", [("box-shadow", "0 2px 4px #000000")]),
    ]
    problems = audit_stylesheet(rules, brand)
    assert any("forbids shadows" in p for p in problems)


def test_corner_radius_cap_catches_violations():
    brand = copy.deepcopy(DEFAULT_BRAND)
    rules = brand_stylesheet(brand) + [
        (".sneaky", [("border-radius", "12px")]),
    ]
    problems = audit_stylesheet(rules, brand)
    assert any("corner cap" in p for p in problems)


def test_gradient_rule_catches_violations():
    brand = copy.deepcopy(DEFAULT_BRAND)
    rules = [(".sneaky", [("background", "linear-gradient(#000000, #ffffff)")])]
    problems = audit_stylesheet(rules, brand)
    assert any("forbids gradients" in p for p in problems)


def test_emitted_stylesheet_is_clean_for_default_brand():
    rules = brand_stylesheet(DEFAULT_BRAND)
    assert audit_stylesheet(rules, DEFAULT_BRAND) == []


def test_zero_radius_policy_flattens_chips():
    raw = json.loads((BRANDS / "nordlicht-strict.json").read_text(encoding="utf-8"))
    brand = Brand.model_validate(raw)
    html = render_report(CONTRACT, make_run_report(), brand, CONTRACT_SHA)
    assert "border-radius: 0px" in html
    assert "border-radius: 2px" not in html


def test_emoji_policy_refuses_and_permits():
    # citations render for failed scenarios, so the emoji goes there
    entries = [entry("s1", "label", passed=False,
                     failed_checks=[{"name": "check", "detail": "detail"}])]
    entries[0]["citations"][0]["text"] = "ship it \U0001F680 today"
    run_report = make_run_report("FAIL", entries)
    with pytest.raises(ReportError) as excinfo:
        render_report(CONTRACT, run_report, DEFAULT_BRAND, CONTRACT_SHA)
    assert any("emoji" in p for p in excinfo.value.problems)

    permissive = copy.deepcopy(DEFAULT_BRAND)
    permissive.policy.allow_emoji = True
    html = render_report(CONTRACT, run_report, permissive, CONTRACT_SHA)
    assert "\U0001F680" in html
