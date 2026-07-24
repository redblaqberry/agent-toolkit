"""The compiler is not bound to the domain it was written against.

Every test here runs the same pipeline over a contract from a different
business (refund handling for an online retailer, with its own actions, roles,
escalation triggers, and unit of work) and asserts that nothing invoice-shaped
leaks into the result. If someone re-hardcodes a scenario template, these fail.
"""

import json

import pytest
import yaml
from typer.testing import CliRunner

from discoveryspec import compile_scenarios, gate_export, load_contract, parse_transcript
from discoveryspec import validate_contract
from discoveryspec.cli import app
from tests.conftest import REFUND_APPROVED_PATH, REFUND_TRANSCRIPT_PATH

runner = CliRunner()

# vocabulary from the first example that must never appear in the second one's
# compiled suite; the old compiler carried all of it as template text
FIRST_DOMAIN_WORDS = [
    "invoice", "erp", "purchase order", "vendor", "nordlicht", "ap_clerk",
    "ap_approver", "posting",
]


@pytest.fixture(scope="module")
def refund_transcript():
    return parse_transcript(REFUND_TRANSCRIPT_PATH)


@pytest.fixture(scope="module")
def refund_contract():
    return load_contract(REFUND_APPROVED_PATH)


@pytest.fixture(scope="module")
def refund_scenarios(refund_contract, refund_transcript):
    return compile_scenarios(refund_contract, refund_transcript)


def test_second_domain_contract_validates_clean(refund_contract, refund_transcript):
    report = validate_contract(refund_contract, refund_transcript)
    assert report.exit_code == 0, (report.errors, report.approval_errors)
    assert report.unwired_requirements == []
    assert report.untestable_requirements == []


def test_second_domain_compiles_one_scenario_per_rule(refund_contract, refund_scenarios):
    assert len(refund_scenarios) == len(refund_contract.acceptance_rules) == 10
    assert [s.id for s in refund_scenarios] == [
        r.slug for r in refund_contract.acceptance_rules
    ]


def test_second_domain_keeps_provenance(refund_scenarios, refund_contract, refund_transcript):
    by_id = refund_contract.requirement_by_id()
    turn_numbers = refund_transcript.turn_numbers()
    for scenario in refund_scenarios:
        assert scenario.requirement_ids, scenario.id
        assert scenario.source_turns, scenario.id
        assert set(scenario.source_turns) <= turn_numbers
        for req_id in scenario.requirement_ids:
            assert req_id in by_id


def test_second_domain_tools_all_exist_in_its_own_contract(
    refund_scenarios, refund_contract
):
    actions = {a.action for a in refund_contract.allowed_actions}
    for scenario in refund_scenarios:
        for tool in (
            scenario.expected_tools
            + scenario.forbidden_tools
            + list(scenario.max_tool_calls)
        ):
            assert tool in actions, f"{scenario.id} references unknown tool {tool}"


def test_no_first_domain_vocabulary_leaks_into_the_second(
    refund_contract, refund_transcript
):
    # the strongest statement available: serialize the whole compiled suite and
    # look for the first example's words. A template that was quietly kept would
    # show up here immediately.
    scenarios_payload, config_payload = gate_export(refund_contract, refund_transcript)
    blob = json.dumps([scenarios_payload, config_payload], ensure_ascii=False).lower()
    for word in FIRST_DOMAIN_WORDS:
        assert word not in blob, f"first-domain word {word!r} leaked into the export"


def test_second_domain_carries_its_own_unit_of_work(refund_contract, refund_transcript):
    _, config_payload = gate_export(refund_contract, refund_transcript)
    assert config_payload["slo"] == {
        "p95_latency_ms": 3000,
        "cost_per_task_eur": 0.05,
        "task_unit": "refund request",
    }


def test_second_domain_latency_budget_comes_from_its_own_slo(refund_scenarios):
    case = next(s for s in refund_scenarios if s.id == "interactive-lookup-latency-budget")
    # 3.0s per step across at most 2 steps
    assert case.budgets["max_latency_s"] == 6.0
    assert case.budgets["max_steps"] == 2


def test_second_domain_states_what_it_does_not_check(refund_contract, refund_transcript):
    _, config_payload = gate_export(refund_contract, refund_transcript)
    not_covered = config_payload["not_covered"]
    assert [entry["requirement_id"] for entry in not_covered] == ["REQ-011"]
    assert "immutable" in not_covered[0]["reason"].lower()
    assert not_covered[0]["verified_by"].strip()


def test_second_domain_exports_through_the_cli(tmp_path):
    out = tmp_path / "export"
    result = runner.invoke(app, [
        "export-gate", "--contract", str(REFUND_APPROVED_PATH), "--out", str(out),
    ])
    assert result.exit_code == 0, result.output + (result.stderr or "")
    data = yaml.safe_load((out / "scenarios.yaml").read_text(encoding="utf-8"))
    assert len(data["scenarios"]) == 10
    assert data["scenarios"][0]["id"] == "refund-under-threshold-completes-autonomously"


def test_second_domain_export_loads_in_agent_eval_gate(tmp_path):
    agent_eval_gate = pytest.importorskip(
        "agent_eval_gate", reason="agent-eval-gate not installed; cross-repo check skipped"
    )
    out = tmp_path / "export"
    assert runner.invoke(app, [
        "export-gate", "--contract", str(REFUND_APPROVED_PATH), "--out", str(out),
    ]).exit_code == 0

    gate_scenarios = agent_eval_gate.load_scenarios(out / "scenarios.yaml")
    assert len(gate_scenarios) == 10
    by_id = {s.id: s for s in gate_scenarios}
    happy = by_id["refund-under-threshold-completes-autonomously"]
    assert happy.checks.expected_tools_ordered
    assert happy.checks.expected_tools[0].name == "read_order"
    assert happy.checks.expected_tools[0].args_subset == {"order_id": "ORD-88120"}


def test_second_domain_rejects_a_rule_naming_a_first_domain_action(tmp_path):
    # the actions a rule may name come from the contract it lives in, so an
    # invoice action in a refund contract is refused rather than silently run
    data = json.loads(REFUND_APPROVED_PATH.read_text(encoding="utf-8"))
    data["acceptance_rules"][0]["expect"]["actions"].append(
        {"name": "post_invoice_to_erp"}
    )
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "transcript.md").write_bytes(REFUND_TRANSCRIPT_PATH.read_bytes())

    result = runner.invoke(app, [
        "export-gate", "--contract", str(contract_path), "--out", str(tmp_path / "x"),
    ])
    assert result.exit_code == 2
    assert "not in the contract's allowed_actions" in result.output + (result.stderr or "")
