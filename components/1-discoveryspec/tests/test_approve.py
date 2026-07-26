"""Human sign-off behavior: only a fully resolved draft can be stamped
approved; the stamp pins the transcript, preserves the reviewed document
exactly, and always produces a contract that passes --require-approved."""

import datetime
import json
import shutil

import pytest
from typer.testing import CliRunner

from discoveryspec import ApprovalError, approve_contract, parse_transcript
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, DRAFT_PATH, TRANSCRIPT_PATH

runner = CliRunner()

APPROVED_BY = "Anna Lindqvist (Operations), Jonas Weber (Security), Priya Nair (Finance)"
APPROVED_AT = "2026-07-16"


def combined(result) -> str:
    return result.output + (result.stderr or "")


@pytest.fixture()
def resolved_draft(approved_dict) -> dict:
    """The golden approved contract with the sign-off stripped: exactly what
    a human hands to ``approve`` after finishing the review."""
    meta = approved_dict["metadata"]
    meta["status"] = "draft"
    meta["approved_by"] = None
    meta["approved_at"] = None
    meta["transcript_sha256"] = None
    return approved_dict


def workdir(tmp_path, contract_dict, with_transcript=True):
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps(contract_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if with_transcript:
        shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    return contract


def approve_cli(contract_path, out, *extra):
    return runner.invoke(app, [
        "approve", "--contract", str(contract_path), "--out", str(out),
        "--by", APPROVED_BY, "--date", APPROVED_AT, *extra,
    ])


# --- the happy path -----------------------------------------------------------

def test_approve_reproduces_golden_approved_contract(tmp_path, resolved_draft):
    # stripping the sign-off from the golden approved contract and stamping it
    # again must reproduce the golden byte for byte: approve touches metadata
    # and nothing else
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 0, combined(result)
    assert out.read_bytes() == APPROVED_PATH.read_bytes()
    assert "approved by: " in result.output
    assert "transcript pinned: sha256" in result.output


def test_approve_refuses_a_draft_that_drops_an_adopted_promise(
    tmp_path, resolved_draft
):
    # the reviewer agreed to something and then never wired it into the
    # contract; signing that off would put a promise nobody tests into a
    # document that claims every promise is tested
    resolved_draft["requirements"].append({
        "id": "REQ-099",
        "title": "Invoice data never leaves EU-hosted infrastructure",
        "statement": "Nothing we process may be stored or served outside the EU.",
        "category": "security",
        "stakeholder": "Jonas Weber (Security)",
        "source_turns": resolved_draft["requirements"][0]["source_turns"],
        "status": "resolved",
        "conflicts_with": [],
        "resolution": None,
    })
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 1, combined(result)
    assert "adopted but not wired into any executable section: REQ-099" in combined(result)
    assert not out.exists()


def test_approve_refuses_a_draft_with_an_untested_behavioral_promise(
    tmp_path, resolved_draft
):
    """The sign-off gate, not just the validator, has to stop this.

    A contract can be perfectly consistent and still promise behavior that no
    acceptance rule checks and nobody has excused. Signing that off would put a
    promise nothing tests inside a document whose whole claim is the opposite.
    """
    resolved_draft["acceptance_rules"] = [
        r for r in resolved_draft["acceptance_rules"] if r["id"] != "RULE-007"
    ]
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 1, combined(result)
    assert (
        "no acceptance rule and no recorded out-of-band verification: REQ-013"
        in combined(result)
    )
    assert not out.exists()


def test_approve_accepts_the_same_promise_when_it_is_excused_out_of_band(
    tmp_path, resolved_draft
):
    # the escape hatch is real, but it has to be written down and signed for
    resolved_draft["acceptance_rules"] = [
        r for r in resolved_draft["acceptance_rules"] if r["id"] != "RULE-007"
    ]
    for req in resolved_draft["requirements"]:
        if req["id"] == "REQ-013":
            req["out_of_band_verification"] = {
                "reason": "Prompt-injection resistance is covered by the platform "
                          "input filter, ahead of the agent.",
                "verified_by": "Nordlicht IT security review before go-live",
            }
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    assert approve_cli(contract, out).exit_code == 0
    assert out.exists()


def test_approve_refuses_a_draft_that_already_carries_an_attestation(
    tmp_path, resolved_draft
):
    # approve is what signs. A signature on an unapproved document did not come
    # from this pipeline, and stamping it would launder that block into an
    # approved artifact while the command reports the approval as unsigned
    resolved_draft["metadata"]["approval_signature"] = {
        "version": "attest.v1",
        "kind": "deployment-contract",
        "algorithm": "ed25519",
        "public_key_fingerprint": "deadbeefdeadbeef",
        "digest_sha256": "0" * 64,
        "signature": "Zm9yZ2Vk",
    }
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 2, combined(result)
    assert "only approve signs a contract" in combined(result)
    assert not out.exists()


def test_approved_output_passes_require_approved(tmp_path, resolved_draft):
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    assert approve_cli(contract, out).exit_code == 0
    check = runner.invoke(app, [
        "validate", "--contract", str(out), "--require-approved",
    ])
    assert check.exit_code == 0, combined(check)


def test_approved_output_exports_gate_suite(tmp_path, resolved_draft):
    import yaml

    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    assert approve_cli(contract, out).exit_code == 0
    export = runner.invoke(app, [
        "export-gate", "--contract", str(out), "--out", str(tmp_path / "export"),
    ])
    assert export.exit_code == 0, combined(export)
    data = yaml.safe_load((tmp_path / "export" / "scenarios.yaml").read_text(encoding="utf-8"))
    assert len(data["scenarios"]) == 10


def test_approve_defaults_to_today(tmp_path, resolved_draft):
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    before = datetime.date.today().isoformat()
    result = runner.invoke(app, [
        "approve", "--contract", str(contract), "--out", str(out), "--by", APPROVED_BY,
    ])
    after = datetime.date.today().isoformat()
    assert result.exit_code == 0, combined(result)
    stamped = json.loads(out.read_text(encoding="utf-8"))
    assert stamped["metadata"]["approved_at"] in {before, after}


def test_nonblocking_open_question_does_not_block_approval(tmp_path, resolved_draft):
    resolved_draft["open_questions"].append({
        "id": "UNK-002",
        "question": "Should rejected invoices be archived in the DMS as well?",
        "field": "notes",
        "owner": "Anna Lindqvist",
        "blocking": False,
        "source_turns": [40],
        "status": "open",
        "resolution": None,
    })
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 0, combined(result)
    assert "open question (non-blocking) stays open past approval: UNK-002" in result.output
    check = runner.invoke(app, ["validate", "--contract", str(out), "--require-approved"])
    assert check.exit_code == 0, combined(check)


# --- refusals: unresolved review state (exit 1) --------------------------------

def test_approve_refuses_draft_with_open_review_state(tmp_path):
    # the golden draft has all three: open conflicts, a blocking question,
    # and pending fields; every one must be named in the refusal
    out = tmp_path / "approved.json"
    result = approve_cli(DRAFT_PATH, out)
    assert result.exit_code == 1
    text = combined(result)
    assert "approval refused" in text
    assert "open conflict: REQ-004 vs REQ-005" in text
    assert "blocking open question UNK-001" in text
    assert "pending required field: data_governance" in text
    assert not out.exists()


def test_approve_refuses_reopened_blocking_question(tmp_path, resolved_draft):
    question = resolved_draft["open_questions"][0]
    question["status"] = "open"
    question["resolution"] = None
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 1
    assert "blocking open question UNK-001" in combined(result)
    assert not out.exists()


def test_approve_refuses_pending_field(tmp_path, resolved_draft):
    resolved_draft["data_governance"] = None
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 1
    assert "pending required field: data_governance" in combined(result)
    assert not out.exists()


def test_approve_refuses_already_approved(tmp_path):
    out = tmp_path / "approved.json"
    result = approve_cli(APPROVED_PATH, out)
    assert result.exit_code == 1
    assert "already approved" in combined(result)
    assert not out.exists()


# --- refusals: structural and sign-off errors (exit 2) -------------------------

def test_approve_refuses_structural_error(tmp_path, resolved_draft):
    resolved_draft["kpis"][0]["requirement_id"] = "REQ-999"
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 2
    assert "unknown requirement REQ-999" in combined(result)
    assert not out.exists()


def test_approve_refuses_transcript_sha_mismatch(tmp_path, resolved_draft):
    resolved_draft["metadata"]["transcript_sha256"] = "ab" * 32
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = approve_cli(contract, out)
    assert result.exit_code == 2
    assert "sha256 mismatch" in combined(result)
    assert not out.exists()


def test_approve_missing_transcript_exit_2(tmp_path, resolved_draft):
    contract = workdir(tmp_path, resolved_draft, with_transcript=False)
    result = approve_cli(contract, tmp_path / "approved.json")
    assert result.exit_code == 2


def test_approve_unreadable_contract_exit_2(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    result = approve_cli(bad, tmp_path / "approved.json")
    assert result.exit_code == 2


def test_approve_refuses_blank_approver(tmp_path, resolved_draft):
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = runner.invoke(app, [
        "approve", "--contract", str(contract), "--out", str(out),
        "--by", "   ", "--date", APPROVED_AT,
    ])
    assert result.exit_code == 2
    assert "cannot be blank" in combined(result)
    assert not out.exists()


@pytest.mark.parametrize("bad_date", ["2026-02-31", "16.07.2026", "yesterday"])
def test_approve_refuses_bad_date(tmp_path, resolved_draft, bad_date):
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    result = runner.invoke(app, [
        "approve", "--contract", str(contract), "--out", str(out),
        "--by", APPROVED_BY, "--date", bad_date,
    ])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in combined(result)
    assert not out.exists()


def test_approve_never_overwrites_the_transcript(tmp_path, resolved_draft):
    # --out aimed at the transcript would destroy the provenance source the
    # command just verified; not even --force may do that
    contract = workdir(tmp_path, resolved_draft)
    transcript_file = tmp_path / "transcript.md"
    original = transcript_file.read_bytes()
    for extra in ((), ("--force",)):
        result = approve_cli(contract, transcript_file, *extra)
        assert result.exit_code == 2
        assert "provenance source" in combined(result)
        assert transcript_file.read_bytes() == original


def test_approve_write_failure_is_exit_2(tmp_path, resolved_draft):
    # an unwritable --out (here: an existing directory forced past the
    # existence check) is an infrastructure failure: exit 2, not a traceback
    contract = workdir(tmp_path, resolved_draft)
    out_dir = tmp_path / "outdir"
    out_dir.mkdir()
    result = approve_cli(contract, out_dir, "--force")
    assert result.exit_code == 2
    assert "cannot write" in combined(result)


def test_approve_never_overwrites_the_draft_input(tmp_path, resolved_draft):
    # the unsigned draft is the record of what was reviewed; stamping over it
    # would destroy the before-state of the sign-off, so not even --force may
    contract = workdir(tmp_path, resolved_draft)
    original = contract.read_bytes()
    for extra in ((), ("--force",)):
        result = approve_cli(contract, contract, *extra)
        assert result.exit_code == 2
        assert "never overwritten" in combined(result)
        assert contract.read_bytes() == original


def test_approve_transcript_guard_catches_hard_links(tmp_path, resolved_draft):
    # a hard link is the same file under a different name; the guard must
    # compare file identity, not just resolved paths
    import os

    contract = workdir(tmp_path, resolved_draft)
    transcript_file = tmp_path / "transcript.md"
    linked = tmp_path / "innocent-name.json"
    try:
        os.link(transcript_file, linked)
    except OSError:
        pytest.skip("filesystem does not support hard links")
    original = transcript_file.read_bytes()
    result = approve_cli(contract, linked, "--force")
    assert result.exit_code == 2
    assert "provenance source" in combined(result)
    assert transcript_file.read_bytes() == original


def test_approve_exclusive_create_catches_write_race(tmp_path, resolved_draft, monkeypatch):
    # simulate a concurrent approval that wins the race between the existence
    # check and the write: the exists() probe says the path is free, but the
    # file is there by write time; exclusive creation must refuse, not truncate
    from pathlib import Path

    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    out.write_text("sentinel: the racing sign-off", encoding="utf-8")

    real_exists = Path.exists

    def exists_racing(self, **kwargs):
        if self == out:
            return False
        return real_exists(self, **kwargs)

    monkeypatch.setattr(Path, "exists", exists_racing)
    result = approve_cli(contract, out)
    assert result.exit_code == 2
    assert "already exists" in combined(result)
    assert out.read_text(encoding="utf-8") == "sentinel: the racing sign-off"


def test_approve_never_overwrites_without_force(tmp_path, resolved_draft):
    contract = workdir(tmp_path, resolved_draft)
    out = tmp_path / "approved.json"
    out.write_text("sentinel: an earlier sign-off artifact", encoding="utf-8")

    refused = approve_cli(contract, out)
    assert refused.exit_code == 2
    assert "already exists" in combined(refused)
    assert out.read_text(encoding="utf-8") == "sentinel: an earlier sign-off artifact"

    forced = approve_cli(contract, out, "--force")
    assert forced.exit_code == 0, combined(forced)
    assert json.loads(out.read_text(encoding="utf-8"))["metadata"]["status"] == "approved"


# --- library-level behavior -----------------------------------------------------

def test_approve_contract_does_not_mutate_input(resolved_draft, transcript):
    from discoveryspec import DeploymentContract

    contract = DeploymentContract.model_validate(resolved_draft)
    stamped = approve_contract(resolved_draft, contract, transcript, APPROVED_BY, APPROVED_AT)
    assert resolved_draft["metadata"]["status"] == "draft"
    assert resolved_draft["metadata"]["approved_by"] is None
    assert stamped["metadata"]["status"] == "approved"
    assert stamped["metadata"]["transcript_sha256"] == transcript.sha256


def test_approve_contract_refusal_carries_exit_code(approved_dict, transcript):
    from discoveryspec import DeploymentContract

    contract = DeploymentContract.model_validate(approved_dict)
    with pytest.raises(ApprovalError) as excinfo:
        approve_contract(approved_dict, contract, transcript, APPROVED_BY, APPROVED_AT)
    assert excinfo.value.exit_code == 1
    assert "already approved" in str(excinfo.value)
