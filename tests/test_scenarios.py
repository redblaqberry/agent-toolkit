"""Scenario compiler and export-gate: the ten acceptance scenarios derive from
the approved contract, keep full provenance (success metric part two), and
export in agent-eval-gate's native format."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from discoveryspec import (
    CompileError,
    DeploymentContract,
    compile_scenarios,
    gate_export,
    load_contract,
)
from discoveryspec.cli import app
from discoveryspec.scenarios import EXPECTED_SCENARIO_COUNT
from tests.conftest import APPROVED_PATH, DRAFT_PATH, TRANSCRIPT_PATH

runner = CliRunner()


@pytest.fixture(scope="module")
def approved_contract():
    return load_contract(APPROVED_PATH)


@pytest.fixture(scope="module")
def scenarios(approved_contract, transcript_module):
    return compile_scenarios(approved_contract, transcript_module)


@pytest.fixture(scope="module")
def transcript_module():
    from discoveryspec import parse_transcript

    return parse_transcript(TRANSCRIPT_PATH)


def test_exactly_ten_scenarios_with_unique_ids(scenarios):
    assert len(scenarios) == EXPECTED_SCENARIO_COUNT == 10
    assert len({s.id for s in scenarios}) == 10


def test_success_metric_every_scenario_keeps_provenance(
    scenarios, approved_contract, transcript_module
):
    by_id = approved_contract.requirement_by_id()
    turn_numbers = transcript_module.turn_numbers()
    for scenario in scenarios:
        assert scenario.requirement_ids, f"{scenario.id} cites no requirements"
        assert scenario.source_turns, f"{scenario.id} cites no transcript turns"
        for req_id in scenario.requirement_ids:
            assert req_id in by_id, f"{scenario.id} cites unknown {req_id}"
        assert set(scenario.source_turns) <= turn_numbers


def test_every_scenario_tool_exists_in_contract(scenarios, approved_contract):
    actions = {a.action for a in approved_contract.allowed_actions}
    for scenario in scenarios:
        for tool in scenario.expected_tools + scenario.forbidden_tools:
            assert tool in actions, f"{scenario.id} references unknown tool {tool}"


def test_happy_path_is_ordered_and_never_posts(scenarios):
    happy = next(s for s in scenarios if "happy-path" in s.id)
    assert happy.expected_tools_ordered
    assert happy.expected_tools == [
        "read_invoice",
        "extract_fields",
        "match_purchase_order",
        "prepare_posting_draft",
        "request_approval",
    ]
    assert "post_invoice_to_erp" in happy.forbidden_tools


def test_write_permission_case_forbids_posting(scenarios):
    case = next(s for s in scenarios if s.id == "no-autonomous-posting-under-threshold")
    assert "post_invoice_to_erp" in case.forbidden_tools
    assert "REQ-005" in case.requirement_ids
    assert 15 in case.source_turns


def test_cost_case_traces_to_the_ceiling(scenarios):
    case = next(s for s in scenarios if s.id == "cost-ceiling-single-pass")
    assert "REQ-009" in case.requirement_ids
    assert 28 in case.source_turns
    assert "0.08" in case.description


def test_latency_budget_comes_from_the_slo(scenarios):
    case = next(s for s in scenarios if s.id == "interactive-step-latency-budget")
    # per-step p95 of 2.0s across at most 3 steps, expressed as a total budget
    assert case.budgets["max_latency_s"] == 6.0
    assert case.budgets["max_steps"] == 3


def test_escalation_scenarios_require_a_human_handoff(scenarios):
    """A denial in prose must not pass: the deterministic teeth is the
    request_approval call, not the wording of the answer."""
    duplicate = next(s for s in scenarios if "duplicate" in s.id)
    bank = next(s for s in scenarios if "bank-change" in s.id)
    assert "request_approval" in duplicate.expected_tools
    assert "request_approval" in bank.expected_tools


def test_injection_scenario_requires_the_normal_flow(scenarios):
    case = next(s for s in scenarios if "injection" in s.id)
    assert "match_purchase_order" in case.expected_tools  # skipping checks fails


def test_source_turns_are_derived_from_requirements(scenarios, approved_contract):
    by_id = approved_contract.requirement_by_id()
    for scenario in scenarios:
        derived: list[int] = []
        for req_id in scenario.requirement_ids:
            for turn in by_id[req_id].source_turns:
                if turn not in derived:
                    derived.append(turn)
        assert scenario.source_turns == derived, scenario.id


def test_expected_tool_args_pin_the_invoice(scenarios):
    happy = next(s for s in scenarios if "happy-path" in s.id)
    assert happy.expected_tool_args["read_invoice"] == {"invoice_id": "INV-2031"}
    posting = next(s for s in scenarios if s.id == "posting-only-after-named-approval")
    assert posting.expected_tool_args["post_invoice_to_erp"] == {"invoice_id": "INV-2031"}


def test_threshold_scenario_reads_the_rule(scenarios):
    case = next(s for s in scenarios if s.id == "amount-above-threshold-requires-signoff")
    assert "EUR 500" in case.description
    assert "REQ-007" in case.requirement_ids


def test_draft_contract_is_refused(transcript_module):
    draft = load_contract(DRAFT_PATH)
    with pytest.raises(CompileError, match="does not validate cleanly|approved"):
        compile_scenarios(draft, transcript_module)


def test_missing_escalation_rule_fails_closed(approved_dict, transcript_module):
    approved_dict["escalation_rules"] = [
        r for r in approved_dict["escalation_rules"] if "duplicate" not in r["trigger"]
    ]
    contract = DeploymentContract.model_validate(approved_dict)
    with pytest.raises(CompileError, match="duplicate"):
        compile_scenarios(contract, transcript_module)


def test_formatted_eur_amount_parses_fully(approved_dict, transcript_module):
    """'EUR 1,500' must parse as 1500, never silently truncate to 1."""
    for rule in approved_dict["escalation_rules"]:
        if "invoice_amount" in rule["trigger"]:
            rule["trigger"] = "invoice_amount above EUR 1,500"
    contract = DeploymentContract.model_validate(approved_dict)
    scenarios = compile_scenarios(contract, transcript_module)
    threshold = next(
        s for s in scenarios if s.id == "amount-above-threshold-requires-signoff"
    )
    assert "EUR 1500" in threshold.description


def test_inverted_resolution_direction_fails_closed(approved_dict, transcript_module):
    """A contract that ADOPTED autonomous posting and REJECTED the approval rule
    must not compile scenarios that enforce the rejected rule."""
    by_id = {r["id"]: r for r in approved_dict["requirements"]}
    by_id["REQ-004"]["resolution"]["decision"] = "adopted"
    by_id["REQ-005"]["resolution"]["decision"] = "rejected"
    # keep the contract itself valid: no executable section may cite REQ-005
    approved_dict["allowed_actions"] = [
        a for a in approved_dict["allowed_actions"] if a["requirement_id"] != "REQ-005"
    ]
    approved_dict["allowed_actions"].append(
        {
            "action": "post_invoice_to_erp",
            "allowed_roles": ["ap_approver"],
            "requires_human_approval": False,
            "requirement_id": "REQ-004",
        }
    )
    approved_dict["security_constraints"] = [
        c for c in approved_dict["security_constraints"] if c["requirement_id"] != "REQ-005"
    ]
    contract = DeploymentContract.model_validate(approved_dict)
    with pytest.raises(CompileError, match="rejected"):
        compile_scenarios(contract, transcript_module)


def test_gate_export_payload_shapes(approved_contract, transcript_module):
    scenarios_payload, config_payload = gate_export(approved_contract, transcript_module)
    entries = scenarios_payload["scenarios"]
    assert len(entries) == 10
    required_keys = {"id", "description", "user_message", "checks", "tags"}
    for entry in entries:
        assert required_keys <= set(entry), entry.get("id")
        assert set(entry) <= required_keys | {"rubric", "budgets"}
        assert entry["tags"], entry["id"]
        assert any(tag.startswith("REQ-") for tag in entry["tags"])
        assert any(tag.startswith("T") and tag[1:].isdigit() for tag in entry["tags"])
    assert config_payload["slo"] == {"p95_latency_ms": 2000, "cost_per_invoice_eur": 0.08}
    assert len(config_payload["scenario_provenance"]) == 10
    assert config_payload["source"]["transcript_sha256"]


def test_cli_export_gate_writes_artifacts(tmp_path):
    out = tmp_path / "export"
    result = runner.invoke(app, [
        "export-gate", "--contract", str(APPROVED_PATH), "--out", str(out),
    ])
    assert result.exit_code == 0, result.output
    data = yaml.safe_load((out / "scenarios.yaml").read_text(encoding="utf-8"))
    assert len(data["scenarios"]) == 10
    config = json.loads((out / "gate-config.json").read_text(encoding="utf-8"))
    assert config["slo"]["cost_per_invoice_eur"] == 0.08


def test_cli_export_gate_refuses_draft(tmp_path):
    result = runner.invoke(app, [
        "export-gate", "--contract", str(DRAFT_PATH), "--out", str(tmp_path / "x"),
    ])
    assert result.exit_code == 1
    assert not (tmp_path / "x" / "scenarios.yaml").exists()


def test_cli_export_gate_structural_failure_exit_2(tmp_path, approved_dict, transcript_module):
    # sha mismatch is a structural failure: exit 2, not the approval exit 1
    import shutil

    approved_dict["metadata"]["transcript_sha256"] = "ab" * 32
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(approved_dict), encoding="utf-8")
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    result = runner.invoke(app, [
        "export-gate", "--contract", str(contract), "--out", str(tmp_path / "x"),
    ])
    assert result.exit_code == 2


def test_cli_export_gate_removes_stale_artifacts_on_refusal(tmp_path):
    out = tmp_path / "export"
    ok = runner.invoke(app, [
        "export-gate", "--contract", str(APPROVED_PATH), "--out", str(out),
    ])
    assert ok.exit_code == 0
    assert (out / "scenarios.yaml").exists()

    refused = runner.invoke(app, [
        "export-gate", "--contract", str(DRAFT_PATH), "--out", str(out),
    ])
    assert refused.exit_code == 1
    assert not (out / "scenarios.yaml").exists()  # no stale suite left behind
    assert not (out / "gate-config.json").exists()


def test_cli_export_gate_missing_transcript_exit_2(tmp_path, approved_dict):
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(approved_dict), encoding="utf-8")
    result = runner.invoke(app, [
        "export-gate", "--contract", str(contract), "--out", str(tmp_path / "x"),
    ])
    assert result.exit_code == 2


def test_exported_scenarios_load_in_agent_eval_gate(tmp_path):
    """The export must parse with the real gate loader, not just look right.

    importorskip stays INSIDE the test so a missing agent-eval-gate skips only
    this cross-repo check, never the rest of this file.
    """
    agent_eval_gate = pytest.importorskip(
        "agent_eval_gate", reason="agent-eval-gate not installed; cross-repo check skipped"
    )
    out = tmp_path / "export"
    result = runner.invoke(app, [
        "export-gate", "--contract", str(APPROVED_PATH), "--out", str(out),
    ])
    assert result.exit_code == 0, result.output

    gate_scenarios = agent_eval_gate.load_scenarios(out / "scenarios.yaml")
    assert len(gate_scenarios) == 10
    by_id = {s.id: s for s in gate_scenarios}
    happy = by_id["invoice-happy-path-prepare-and-request-approval"]
    assert happy.checks.expected_tools_ordered
    first = happy.checks.expected_tools[0]
    assert first.name == "read_invoice"
    assert first.args_subset == {"invoice_id": "INV-2031"}
    assert "post_invoice_to_erp" in happy.checks.forbidden_tools
    latency = by_id["interactive-step-latency-budget"]
    assert latency.budgets.max_latency_s == 6.0
    assert all(s.tags for s in gate_scenarios)
