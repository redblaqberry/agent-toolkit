"""Human sign-off: stamp a fully resolved draft contract as approved.

``approve`` is the one legitimate way a contract gains ``status: approved``.
It runs the full validator first and refuses anything that is not ready:
structural defects, open conflicts, blocking open questions, unfilled
required fields, and adopted requirements that no executable section
references all block the stamp. On success it sets the sign-off
(``approved_by``, ``approved_at``) and pins the SHA-256 of the transcript
the provenance was just verified against; every other byte of the document
stays exactly as the human reviewed it.

Fail-closed rules:
- an already-approved contract is refused; a sign-off is a record, and
  re-stamping would silently rewrite who approved what and when
- a draft that already carries ``metadata.approval_signature`` is refused:
  approve is what signs, so an attestation on an unapproved document did not
  come from this pipeline and must not be laundered into an approved one
- the sign-off itself is checked: a blank approver or a non-calendar date
  is a hard error, not a warning
- the stamped result is re-validated end to end (JSON Schema, model,
  validator, provenance against the transcript) before it is returned, so
  a document ``approve`` produces always passes ``validate --require-approved``
- non-blocking open questions do not block approval; they survive into the
  approved contract and stay visible in ``validate`` output
"""

from __future__ import annotations

import copy
from datetime import datetime

from .loader import ContractLoadError, contract_from_raw
from .models import DeploymentContract
from .transcript import Transcript
from .validate import validate_contract


class ApprovalError(ValueError):
    """The contract cannot be approved.

    ``exit_code`` mirrors the CLI convention: 1 when the review state blocks
    the sign-off (open conflicts, blocking questions, pending fields, or an
    existing approval), 2 for structural problems or an invalid sign-off.
    """

    def __init__(self, message: str, exit_code: int = 1, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.problems = problems or []


def approve_contract(
    raw: dict,
    contract: DeploymentContract,
    transcript: Transcript,
    approved_by: str,
    approved_at: str,
) -> dict:
    """Return a copy of ``raw`` stamped as approved; ``raw`` is not mutated.

    ``raw`` and ``contract`` must describe the same document (the CLI loads
    both from one file): the raw dict is what gets stamped, so the output
    preserves the reviewed document exactly; the model is what gets validated.
    """
    if not approved_by.strip():
        raise ApprovalError(
            "approved_by must name who signs off; it cannot be blank", exit_code=2
        )
    try:
        datetime.strptime(approved_at, "%Y-%m-%d")
    except ValueError as exc:
        raise ApprovalError(
            f"approved_at must be a real YYYY-MM-DD date, got {approved_at!r}", exit_code=2
        ) from exc

    meta = contract.metadata
    if meta.status == "approved":
        raise ApprovalError(
            f"contract is already approved by {meta.approved_by!r} on {meta.approved_at}; "
            f"refusing to re-stamp an existing sign-off",
            exit_code=1,
        )
    if meta.approval_signature is not None:
        # A draft is never signed: approve is what signs. An attestation on an
        # unapproved document was hand-written or invented by an extractor, and
        # stamping it would launder that block into an approved artifact whose
        # own output reports the approval as unsigned.
        raise ApprovalError(
            "draft carries metadata.approval_signature, but only approve signs a "
            "contract; this attestation was not produced by this pipeline. Remove "
            "it before sign-off",
            exit_code=2,
        )

    report = validate_contract(contract, transcript)
    if report.errors:
        raise ApprovalError(
            "contract has structural errors; fix them before sign-off",
            exit_code=2,
            problems=report.errors,
        )

    blockers: list[str] = []
    for group in report.conflict_groups:
        blockers.append(f"open conflict: {' vs '.join(group)}")
    questions = {q.id: q for q in contract.open_questions}
    for question_id in report.open_blocking_questions:
        question = questions[question_id]
        blockers.append(
            f"blocking open question {question_id} (owner {question.owner}): "
            f"{question.question}"
        )
    for field_name in report.pending_fields:
        blockers.append(f"pending required field: {field_name}")
    by_id = contract.requirement_by_id()
    for req_id in report.unwired_requirements:
        req = by_id[req_id]
        blockers.append(
            f"adopted but not wired into any executable section: {req_id} "
            f"({req.stakeholder}: {req.title})"
        )
    for req_id in report.untestable_requirements:
        req = by_id[req_id]
        blockers.append(
            f"no acceptance rule and no recorded out-of-band verification: {req_id} "
            f"({req.stakeholder}: {req.title})"
        )
    if blockers:
        raise ApprovalError(
            "the discovery review is not finished; resolve these before sign-off",
            exit_code=1,
            problems=blockers,
        )

    stamped = copy.deepcopy(raw)
    stamped_meta = stamped["metadata"]
    stamped_meta["status"] = "approved"
    stamped_meta["approved_by"] = approved_by
    stamped_meta["approved_at"] = approved_at
    stamped_meta["transcript_sha256"] = transcript.sha256

    # Belt and braces: the stamped document must survive the exact pipeline
    # (schema, model, validator, provenance) that CI runs on approved contracts.
    try:
        stamped_contract = contract_from_raw(stamped, source="<stamped contract>")
    except ContractLoadError as exc:
        raise ApprovalError(
            "stamped contract no longer parses; refusing to produce it",
            exit_code=2,
            problems=exc.problems,
        ) from exc
    final = validate_contract(stamped_contract, transcript)
    if final.exit_code != 0:
        raise ApprovalError(
            "stamped contract failed re-validation; refusing to produce it",
            exit_code=2,
            problems=final.errors + final.approval_errors,
        )

    return stamped
