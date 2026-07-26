"""Fail-closed validator behavior: structural errors, approval gating, and
CLI exit codes (0 valid, 1 approval blocked, 2 invalid)."""

import json
import shutil

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from discoveryspec import DeploymentContract, load_contract, validate_contract
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, DRAFT_PATH, FIXTURES, TRANSCRIPT_PATH

runner = CliRunner()


def validate_dict(data: dict, transcript=None):
    return validate_contract(DeploymentContract.model_validate(data), transcript)


def dropped_promise(data: dict) -> dict:
    """Add a requirement the room agreed on that no section ever references."""
    data["requirements"].append({
        "id": "REQ-099",
        "title": "Invoice data never leaves EU-hosted infrastructure",
        "statement": "Nothing we process may be stored or served outside the EU.",
        "category": "security",
        "stakeholder": "Jonas Weber (Security)",
        "source_turns": data["requirements"][0]["source_turns"],
        "status": "resolved",
        "conflicts_with": [],
        "resolution": None,
    })
    return data


# --- adopted promises that were never wired in -------------------------------

def test_approved_contract_cannot_drop_an_adopted_promise(approved_dict, transcript):
    report = validate_dict(dropped_promise(approved_dict), transcript)
    assert report.unwired_requirements == ["REQ-099"]
    assert report.exit_code == 1
    assert any("no executable section references" in e for e in report.approval_errors)


def test_draft_reports_an_unwired_requirement_without_failing(draft_dict, transcript):
    # a draft is allowed to be half wired: that is the reviewer's work in
    # progress, so it is a finding, not a failure
    report = validate_dict(dropped_promise(draft_dict), transcript)
    assert report.unwired_requirements == ["REQ-099"]
    assert report.exit_code == 0


def test_rejected_requirements_are_not_reported_as_unwired(approved_dict, transcript):
    # the golden approved contract rejects three requirements; a rejected
    # promise must never be wired in, so it is not a dropped one either
    report = validate_dict(approved_dict, transcript)
    assert report.unwired_requirements == []
    rejected = [
        r["id"] for r in approved_dict["requirements"]
        if (r.get("resolution") or {}).get("decision") == "rejected"
    ]
    assert len(rejected) == 3


def test_open_conflicts_are_not_reported_as_unwired(draft_dict, transcript):
    # the six requirements in open conflict in the golden draft are unwired by
    # definition: which side gets wired is exactly what resolving them decides
    report = validate_dict(draft_dict, transcript)
    assert report.unwired_requirements == []
    assert report.conflict_groups


def test_unwired_requirement_blocks_the_cli(tmp_path, approved_dict, transcript):
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(dropped_promise(approved_dict), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    result = runner.invoke(app, ["validate", "--contract", str(contract_path)])
    assert result.exit_code == 1
    assert "ADOPTED BUT NOT WIRED IN (1)" in result.output
    assert "REQ-099" in result.output


# --- structural errors (exit 2 territory) ------------------------------------

def test_dangling_turn_fixture_fails(transcript):
    data = json.loads((FIXTURES / "bad-dangling-turn.json").read_text(encoding="utf-8"))
    report = validate_dict(data, transcript)
    assert report.exit_code == 2
    assert any("T99" in e for e in report.errors)


def test_duplicate_ids_fixture_fails():
    data = json.loads((FIXTURES / "bad-duplicate-ids.json").read_text(encoding="utf-8"))
    report = validate_dict(data)
    assert report.exit_code == 2
    assert any("duplicate requirement id" in e for e in report.errors)


def test_missing_provenance_fails(draft_dict):
    draft_dict["requirements"][0]["source_turns"] = []
    draft_dict["requirements"][0]["followup_note"] = None
    report = validate_dict(draft_dict)
    assert any("no provenance" in e for e in report.errors)


def test_executable_ref_to_unknown_requirement_fails(draft_dict):
    draft_dict["kpis"][0]["requirement_id"] = "REQ-999"
    report = validate_dict(draft_dict)
    assert any("unknown requirement REQ-999" in e for e in report.errors)


def test_executable_ref_to_conflicted_requirement_fails(draft_dict):
    # REQ-004 is an open conflict in the draft; wiring it into an executable
    # section must be rejected: only resolved requirements are executable.
    draft_dict["allowed_actions"][0]["requirement_id"] = "REQ-004"
    report = validate_dict(draft_dict)
    assert any("status is conflict" in e for e in report.errors)


def test_action_with_unknown_role_fails(draft_dict):
    draft_dict["allowed_actions"][0]["allowed_roles"] = ["cfo"]
    report = validate_dict(draft_dict)
    assert any("unknown roles ['cfo']" in e for e in report.errors)


def test_asymmetric_conflict_fails(draft_dict):
    by_id = {r["id"]: r for r in draft_dict["requirements"]}
    by_id["REQ-005"]["conflicts_with"] = []
    by_id["REQ-005"]["status"] = "resolved"
    report = validate_dict(draft_dict)
    assert any("must be symmetric" in e for e in report.errors)


def test_conflict_status_without_partners_fails(draft_dict):
    by_id = {r["id"]: r for r in draft_dict["requirements"]}
    by_id["REQ-004"]["conflicts_with"] = []
    by_id["REQ-005"]["conflicts_with"] = []
    by_id["REQ-005"]["status"] = "resolved"
    report = validate_dict(draft_dict)
    assert any("conflicts_with is empty" in e for e in report.errors)


def test_resolution_with_conflict_status_fails(draft_dict):
    by_id = {r["id"]: r for r in draft_dict["requirements"]}
    by_id["REQ-004"]["resolution"] = {
        "decision": "rejected",
        "rationale": "x",
        "resolved_by": "y",
        "date": "2026-07-15",
    }
    report = validate_dict(draft_dict)
    assert any("status is still conflict" in e for e in report.errors)


def test_resolved_conflict_without_record_fails(approved_dict):
    by_id = {r["id"]: r for r in approved_dict["requirements"]}
    by_id["REQ-004"]["resolution"] = None
    report = validate_dict(approved_dict)
    assert any("no resolution record" in e for e in report.errors)


def test_resolved_question_without_record_fails(approved_dict):
    approved_dict["open_questions"][0]["resolution"] = None
    report = validate_dict(approved_dict)
    assert any("without a resolution record" in e for e in report.errors)


def test_rejected_requirement_is_not_executable(approved_dict):
    # REQ-004 (autonomous posting) was resolved as rejected; wiring it into an
    # executable section must fail: rejected customer asks cannot deploy.
    approved_dict["allowed_actions"][0]["requirement_id"] = "REQ-004"
    report = validate_dict(approved_dict)
    assert report.exit_code == 2
    assert any("REJECTED" in e for e in report.errors)


def test_both_sides_of_a_conflict_cannot_be_adopted(approved_dict):
    by_id = {r["id"]: r for r in approved_dict["requirements"]}
    by_id["REQ-004"]["resolution"]["decision"] = "adopted"  # REQ-005 is adopted too
    report = validate_dict(approved_dict)
    assert any("both adopted" in e for e in report.errors)


def test_duplicate_action_names_fail(draft_dict):
    twin = dict(draft_dict["allowed_actions"][0])
    twin["requires_human_approval"] = True
    draft_dict["allowed_actions"].append(twin)
    report = validate_dict(draft_dict)
    assert any("duplicate allowed action" in e for e in report.errors)


def test_duplicate_kpi_names_fail(draft_dict):
    draft_dict["kpis"].append(dict(draft_dict["kpis"][0]))
    report = validate_dict(draft_dict)
    assert any("duplicate kpi name" in e for e in report.errors)


def test_section_category_mismatch_fails(draft_dict):
    # a KPI backed by a security requirement is a wrong provenance link
    draft_dict["kpis"][0]["requirement_id"] = "REQ-011"
    report = validate_dict(draft_dict)
    assert any("category security" in e for e in report.errors)


# --- schema/model strictness: both layers reject the same documents -----------

def test_model_rejects_turn_number_zero(draft_dict):
    draft_dict["requirements"][0]["source_turns"] = [0]
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(draft_dict)


def test_model_rejects_non_snake_case_permissions(draft_dict):
    draft_dict["roles"][0]["permissions"] = ["Release Posting"]
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(draft_dict)


def test_model_rejects_missing_required_sections(draft_dict):
    draft_dict.pop("open_questions")
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(draft_dict)


def test_model_rejects_malformed_approval_date(approved_dict):
    approved_dict["metadata"]["approved_at"] = "July 16, 2026"
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(approved_dict)


def test_loader_rejects_nan_constants(tmp_path):
    bad = tmp_path / "nan.json"
    bad.write_text('{"contract_version": NaN}', encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        load_contract(bad)
    assert "non-standard JSON constant" in str(excinfo.value.__cause__ or excinfo.value)


def test_loader_rejects_duplicate_json_keys(tmp_path):
    bad = tmp_path / "dup.json"
    bad.write_text('{"status": "draft", "status": "approved"}', encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        load_contract(bad)
    assert "duplicate JSON key" in str(excinfo.value.__cause__ or excinfo.value)


def test_model_rejects_calendar_invalid_dates(approved_dict):
    approved_dict["metadata"]["approved_at"] = "2026-99-99"
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(approved_dict)


def test_whitespace_followup_note_is_not_provenance(draft_dict):
    draft_dict["requirements"][0]["source_turns"] = []
    draft_dict["requirements"][0]["followup_note"] = "   "
    report = validate_dict(draft_dict)
    assert any("no provenance" in e for e in report.errors)


def test_transcript_sha256_mismatch_fails(draft_dict, transcript):
    draft_dict["metadata"]["transcript_sha256"] = "ab" * 32
    report = validate_dict(draft_dict, transcript)
    assert report.exit_code == 2
    assert any("sha256 mismatch" in e for e in report.errors)


def test_resulting_requirement_needs_a_provenance_link(approved_dict):
    # REQ-001 exists but has neither follow-up provenance nor shared turns
    # with UNK-001, so claiming it resulted from the question is unsupported
    approved_dict["open_questions"][0]["resolution"]["resulting_requirements"] = ["REQ-001"]
    report = validate_dict(approved_dict)
    assert any("unsubstantiated" in e for e in report.errors)


def test_schema_and_model_agree_on_required_fields(draft_dict):
    """Both layers must reject the same documents (schema parity)."""
    from jsonschema import Draft202012Validator

    from discoveryspec import load_schema

    schema_validator = Draft202012Validator(load_schema())

    missing_turns = json.loads(json.dumps(draft_dict))
    missing_turns["requirements"][0].pop("source_turns")
    assert list(schema_validator.iter_errors(missing_turns))
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(missing_turns)

    missing_resulting = json.loads(json.dumps(draft_dict))
    missing_resulting["open_questions"][0]["status"] = "resolved"
    missing_resulting["open_questions"][0]["resolution"] = {
        "answer": "x", "resolved_by": "y", "date": "2026-07-15",
    }  # resulting_requirements omitted
    assert list(schema_validator.iter_errors(missing_resulting))
    with pytest.raises(ValidationError):
        DeploymentContract.model_validate(missing_resulting)


# --- approval gating (exit 1 territory) ---------------------------------------

def test_approved_with_open_conflict_fixture_blocks():
    data = json.loads(
        (FIXTURES / "bad-approved-open-conflict.json").read_text(encoding="utf-8")
    )
    report = validate_dict(data)
    assert report.exit_code == 1
    assert any("open conflicts" in e for e in report.approval_errors)


def test_approved_without_signoff_blocks(approved_dict):
    approved_dict["metadata"]["approved_by"] = None
    report = validate_dict(approved_dict)
    assert report.exit_code == 1
    assert any("no approved_by" in e for e in report.approval_errors)


def test_whitespace_signoff_blocks(approved_dict):
    approved_dict["metadata"]["approved_by"] = "   "
    report = validate_dict(approved_dict)
    assert report.exit_code == 1
    assert any("no approved_by" in e for e in report.approval_errors)


def test_approved_without_transcript_pin_blocks(approved_dict):
    approved_dict["metadata"]["transcript_sha256"] = None
    report = validate_dict(approved_dict)
    assert report.exit_code == 1
    assert any("transcript_sha256" in e for e in report.approval_errors)


def test_approved_with_blocking_unknown_blocks(approved_dict):
    approved_dict["open_questions"][0]["status"] = "open"
    approved_dict["open_questions"][0]["resolution"] = None
    report = validate_dict(approved_dict)
    assert any("blocking open questions" in e for e in report.approval_errors)


def test_approved_with_pending_fields_blocks(approved_dict):
    approved_dict["data_governance"] = None
    report = validate_dict(approved_dict)
    assert any("unfilled required fields" in e for e in report.approval_errors)


def test_draft_with_findings_is_not_an_error(draft_dict):
    report = validate_dict(draft_dict)
    assert report.exit_code == 0
    assert report.conflict_groups


# --- CLI exit codes -------------------------------------------------------------

def test_cli_draft_exit_0_and_reports_conflicts():
    result = runner.invoke(app, [
        "validate", "--contract", str(DRAFT_PATH), "--transcript", str(TRANSCRIPT_PATH),
    ])
    assert result.exit_code == 0, result.output
    assert "CONFLICTS (3)" in result.output
    assert "UNK-001" in result.output
    assert "verdict: VALID" in result.output


def test_cli_approved_exit_0_clean():
    result = runner.invoke(app, [
        "validate", "--contract", str(APPROVED_PATH), "--transcript", str(TRANSCRIPT_PATH),
    ])
    assert result.exit_code == 0, result.output
    assert "CONFLICTS" not in result.output


def test_cli_structural_failure_exit_2():
    result = runner.invoke(app, [
        "validate",
        "--contract", str(FIXTURES / "bad-dangling-turn.json"),
        "--transcript", str(TRANSCRIPT_PATH),
    ])
    assert result.exit_code == 2


def test_cli_approval_violation_exit_1():
    result = runner.invoke(app, [
        "validate", "--contract", str(FIXTURES / "bad-approved-open-conflict.json"),
        "--transcript", str(TRANSCRIPT_PATH),
    ])
    assert result.exit_code == 1


def test_cli_unreadable_contract_exit_2(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["validate", "--contract", str(bad)])
    assert result.exit_code == 2


def test_cli_schema_violation_exit_2(tmp_path, draft_dict):
    draft_dict.pop("slo")
    bad = tmp_path / "no-slo.json"
    bad.write_text(json.dumps(draft_dict), encoding="utf-8")
    result = runner.invoke(app, ["validate", "--contract", str(bad)])
    assert result.exit_code == 2


def test_cli_malformed_transcript_exit_2(tmp_path, draft_dict):
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(draft_dict), encoding="utf-8")
    bad = tmp_path / "transcript.md"  # name matches metadata.transcript
    bad.write_text("[T01] A (Ops): one\n[T05] B (Sec): five\n", encoding="utf-8")
    result = runner.invoke(app, [
        "validate", "--contract", str(contract), "--transcript", str(bad),
    ])
    assert result.exit_code == 2


def test_cli_transcript_resolved_from_metadata():
    # draft-contract.json sits next to transcript.md; provenance is checked
    # even without --transcript
    result = runner.invoke(app, ["validate", "--contract", str(DRAFT_PATH)])
    assert result.exit_code == 0, result.output
    assert "CONFLICTS (3)" in result.output


def test_cli_missing_declared_transcript_exit_2(tmp_path, draft_dict):
    contract = tmp_path / "contract.json"  # no transcript.md next to it
    contract.write_text(json.dumps(draft_dict), encoding="utf-8")
    result = runner.invoke(app, ["validate", "--contract", str(contract)])
    assert result.exit_code == 2


def test_cli_transcript_name_mismatch_exit_2(tmp_path):
    other = tmp_path / "other.md"
    shutil.copyfile(TRANSCRIPT_PATH, other)
    result = runner.invoke(app, [
        "validate", "--contract", str(DRAFT_PATH), "--transcript", str(other),
    ])
    assert result.exit_code == 2


def test_cli_same_name_different_transcript_fails_on_sha(tmp_path, draft_dict):
    # same file name, different bytes: the sha256 pin catches the swap
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(draft_dict), encoding="utf-8")
    fake = tmp_path / "transcript.md"
    fake.write_text(
        "\n".join(f"[T{n:02d}] A (Ops): filler turn {n}" for n in range(1, 42)),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", "--contract", str(contract)])
    assert result.exit_code == 2
    assert "sha256 mismatch" in result.output + str(result.stderr or "")


def test_cli_require_approved_blocks_drafts():
    result = runner.invoke(app, [
        "validate", "--contract", str(DRAFT_PATH), "--require-approved",
    ])
    assert result.exit_code == 1


def test_cli_require_approved_passes_clean_approved():
    result = runner.invoke(app, [
        "validate", "--contract", str(APPROVED_PATH), "--require-approved",
    ])
    assert result.exit_code == 0, result.output
