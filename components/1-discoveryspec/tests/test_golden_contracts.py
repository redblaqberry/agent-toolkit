"""The golden contracts are the extraction and approval targets; these tests
encode the project's success metric: all three seeded discovery conflicts and
the blocking data-governance question are surfaced before anything runs."""

from discoveryspec import load_contract, validate_contract
from tests.conftest import APPROVED_PATH, DRAFT_PATH

EXPECTED_CONFLICTS = {
    frozenset({"REQ-004", "REQ-005"}),  # autonomous posting vs named human approval
    frozenset({"REQ-006", "REQ-007"}),  # approver threshold EUR 5000 vs EUR 500
    frozenset({"REQ-008", "REQ-009"}),  # best model regardless vs EUR 0.08 ceiling
}


def test_draft_golden_passes_schema_and_model():
    contract = load_contract(DRAFT_PATH)
    assert contract.metadata.status == "draft"
    assert len(contract.requirements) == 18


def test_success_metric_all_three_seeded_conflicts_surface(transcript):
    contract = load_contract(DRAFT_PATH)
    report = validate_contract(contract, transcript)
    assert report.exit_code == 0, (report.errors, report.approval_errors)
    assert {frozenset(g) for g in report.conflict_groups} == EXPECTED_CONFLICTS


def test_success_metric_blocking_unknown_surfaces(transcript):
    contract = load_contract(DRAFT_PATH)
    report = validate_contract(contract, transcript)
    assert report.open_blocking_questions == ["UNK-001"]
    assert report.open_nonblocking_questions == []


def test_draft_reports_pending_required_fields(transcript):
    contract = load_contract(DRAFT_PATH)
    report = validate_contract(contract, transcript)
    assert set(report.pending_fields) == {"slo.cost_per_task_eur", "data_governance"}


def test_draft_provenance_resolves_against_transcript(transcript):
    contract = load_contract(DRAFT_PATH)
    turn_numbers = transcript.turn_numbers()
    for req in contract.requirements:
        assert req.source_turns, f"{req.id} has no source turns"
        assert set(req.source_turns) <= turn_numbers, f"{req.id} cites missing turns"


def test_approved_golden_is_clean(transcript):
    contract = load_contract(APPROVED_PATH)
    report = validate_contract(contract, transcript)
    assert report.exit_code == 0, (report.errors, report.approval_errors)
    assert report.conflict_groups == []
    assert report.open_blocking_questions == []
    assert report.pending_fields == []


def test_approved_golden_preserves_conflict_history(transcript):
    contract = load_contract(APPROVED_PATH)
    by_id = contract.requirement_by_id()
    for pair in EXPECTED_CONFLICTS:
        pair_decisions = []
        for req_id in pair:
            req = by_id[req_id]
            assert req.status == "resolved"
            assert req.conflicts_with, f"{req_id} lost its conflict trace"
            assert req.resolution is not None, f"{req_id} resolved without a record"
            pair_decisions.append(req.resolution.decision)
        # each conflict pair must resolve coherently: one side wins, one loses
        assert sorted(pair_decisions) == ["adopted", "rejected"], (pair, pair_decisions)


def test_approved_golden_resolves_unknown_via_followup(transcript):
    contract = load_contract(APPROVED_PATH)
    question = contract.open_questions[0]
    assert question.status == "resolved"
    assert question.resolution.resulting_requirements == ["REQ-019"]
    req = contract.requirement_by_id()["REQ-019"]
    assert req.source_turns == []
    assert req.followup_note
    assert contract.data_governance.requirement_id == "REQ-019"


def test_approved_contract_forbids_autonomous_posting():
    contract = load_contract(APPROVED_PATH)
    posting = next(a for a in contract.allowed_actions if a.action == "post_invoice_to_erp")
    assert posting.requires_human_approval is True
    assert posting.allowed_roles == ["ap_approver"]


def test_approved_thresholds_match_resolutions():
    contract = load_contract(APPROVED_PATH)
    assert contract.slo.cost_per_task_eur.value == 0.08
    threshold_rule = next(
        r for r in contract.escalation_rules if r.requirement_id == "REQ-007"
    )
    assert "EUR 500" in threshold_rule.trigger
