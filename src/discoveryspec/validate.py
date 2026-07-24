"""Contract validation: structural integrity, provenance, and approval gating.

Three kinds of output, mirroring agent-eval-gate's fail-closed exit codes:

- ``errors``: structural defects (broken provenance, dangling references,
  contradictory status fields). Exit 2. The contract is not trustworthy.
- ``approval_errors``: the contract claims ``status: approved`` but still has
  open conflicts, blocking open questions, or unfilled required fields.
  Exit 1. No unreviewed or incompletely resolved contract can run.
- findings (``conflict_groups``, ``open_questions``, ``pending_fields``,
  ``unwired_requirements``): expected content of a draft, surfaced for the
  human reviewer. Exit 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    BEHAVIORAL_CATEGORIES,
    BEHAVIORAL_SECTIONS,
    DeploymentContract,
    Requirement,
)
from .transcript import Transcript


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    approval_errors: list[str] = field(default_factory=list)
    conflict_groups: list[tuple[str, ...]] = field(default_factory=list)
    open_blocking_questions: list[str] = field(default_factory=list)
    open_nonblocking_questions: list[str] = field(default_factory=list)
    pending_fields: list[str] = field(default_factory=list)
    unwired_requirements: list[str] = field(default_factory=list)
    untestable_requirements: list[str] = field(default_factory=list)
    provenance_verified: bool = False  # True only when a transcript was checked

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 2
        if self.approval_errors:
            return 1
        return 0


def _conflict_groups(requirements: list[Requirement]) -> list[tuple[str, ...]]:
    """Connected components over open-conflict requirements."""
    open_ids = {r.id for r in requirements if r.status == "conflict"}
    adjacency = {
        r.id: [c for c in r.conflicts_with if c in open_ids]
        for r in requirements
        if r.id in open_ids
    }
    seen: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for start in sorted(open_ids):
        if start in seen:
            continue
        stack, component = [start], set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency.get(node, []))
        seen |= component
        groups.append(tuple(sorted(component)))
    return groups


def validate_contract(
    contract: DeploymentContract,
    transcript: Transcript | None = None,
) -> ValidationReport:
    """Validate a contract, checking turn provenance against ``transcript``.

    ``transcript=None`` skips the turn-existence and sha256 checks (the report
    carries ``provenance_verified=False`` so callers cannot mistake unverified
    for verified). The CLI never allows that path; it always loads a transcript.
    """
    report = ValidationReport()
    errors = report.errors

    requirements = contract.requirements
    by_id = {}
    for req in requirements:
        if req.id in by_id:
            errors.append(f"duplicate requirement id {req.id}")
        by_id[req.id] = req

    question_ids = set()
    for question in contract.open_questions:
        if question.id in question_ids:
            errors.append(f"duplicate open question id {question.id}")
        question_ids.add(question.id)

    turn_numbers = transcript.turn_numbers() if transcript is not None else None
    report.provenance_verified = transcript is not None

    # --- provenance -----------------------------------------------------
    if (
        transcript is not None
        and contract.metadata.transcript_sha256 is not None
        and transcript.sha256 != contract.metadata.transcript_sha256
    ):
        errors.append(
            f"transcript sha256 mismatch: contract pins "
            f"{contract.metadata.transcript_sha256[:12]}..., supplied transcript is "
            f"{transcript.sha256[:12]}...; provenance was extracted from different bytes"
        )
    for req in requirements:
        if not req.source_turns and not (req.followup_note or "").strip():
            errors.append(
                f"{req.id}: no provenance; source_turns is empty and followup_note is not set"
            )
        if turn_numbers is not None:
            missing = sorted(set(req.source_turns) - turn_numbers)
            if missing:
                errors.append(
                    f"{req.id}: source turns not present in transcript: "
                    f"{['T%02d' % n for n in missing]}"
                )
    if turn_numbers is not None:
        for question in contract.open_questions:
            missing = sorted(set(question.source_turns) - turn_numbers)
            if missing:
                errors.append(
                    f"{question.id}: source turns not present in transcript: "
                    f"{['T%02d' % n for n in missing]}"
                )

    # --- conflict bookkeeping -------------------------------------------
    for req in requirements:
        for other_id in req.conflicts_with:
            if other_id == req.id:
                errors.append(f"{req.id}: conflicts with itself")
            elif other_id not in by_id:
                errors.append(f"{req.id}: conflicts_with references unknown {other_id}")
            elif req.id not in by_id[other_id].conflicts_with:
                errors.append(
                    f"{req.id} lists a conflict with {other_id}, "
                    f"but {other_id} does not list {req.id} (must be symmetric)"
                )
        if req.status == "conflict":
            if not req.conflicts_with:
                errors.append(f"{req.id}: status is conflict but conflicts_with is empty")
            if req.resolution is not None:
                errors.append(
                    f"{req.id}: has a resolution but status is still conflict; "
                    f"resolved conflicts must set status resolved"
                )
        if req.status == "resolved" and req.conflicts_with and req.resolution is None:
            errors.append(
                f"{req.id}: resolved out of a conflict with {req.conflicts_with} "
                f"but carries no resolution record"
            )

    # Resolutions across a conflict edge must be coherent: two contradictory
    # requirements cannot both be adopted.
    for req in requirements:
        for other_id in req.conflicts_with:
            other = by_id.get(other_id)
            if other is None or req.id >= other_id:
                continue
            if (
                req.resolution is not None
                and other.resolution is not None
                and req.resolution.decision == "adopted"
                and other.resolution.decision == "adopted"
            ):
                errors.append(
                    f"{req.id} and {other_id} conflict but were both adopted; "
                    f"a contradiction cannot be resolved by adopting both sides"
                )

    # --- open question bookkeeping ----------------------------------------
    for question in contract.open_questions:
        if question.status == "resolved" and question.resolution is None:
            errors.append(f"{question.id}: status resolved without a resolution record")
        if question.status == "open" and question.resolution is not None:
            errors.append(f"{question.id}: has a resolution but status is still open")
        if question.resolution is not None:
            for req_id in question.resolution.resulting_requirements:
                req = by_id.get(req_id)
                if req is None:
                    errors.append(
                        f"{question.id}: resulting requirement {req_id} does not exist"
                    )
                elif not (req.followup_note or "").strip() and not (
                    set(req.source_turns) & set(question.source_turns)
                ):
                    errors.append(
                        f"{question.id}: resulting requirement {req_id} has no follow-up "
                        f"provenance and shares no source turns with the question; "
                        f"the answer-to-requirement link is unsubstantiated"
                    )

    # --- executable sections reference resolved, non-rejected requirements ---
    role_names = set()
    for role in contract.roles:
        if role.name in role_names:
            errors.append(f"duplicate role name {role.name}")
        role_names.add(role.name)
    kpi_names = set()
    for kpi in contract.kpis:
        if kpi.name in kpi_names:
            errors.append(f"duplicate kpi name {kpi.name}")
        kpi_names.add(kpi.name)
    action_names = set()
    for action in contract.allowed_actions:
        if action.action in action_names:
            errors.append(f"duplicate allowed action {action.action}")
        action_names.add(action.action)

    referenced: set[str] = set()
    behavioral_refs: set[str] = set()

    def check_ref(section: str, req_id: str, categories: set[str]) -> None:
        referenced.add(req_id)
        if section.startswith(BEHAVIORAL_SECTIONS):
            behavioral_refs.add(req_id)
        req = by_id.get(req_id)
        if req is None:
            errors.append(f"{section}: references unknown requirement {req_id}")
            return
        if req.status != "resolved":
            errors.append(
                f"{section}: references {req_id} whose status is {req.status}; "
                f"only resolved requirements are executable"
            )
        elif req.resolution is not None and req.resolution.decision == "rejected":
            errors.append(
                f"{section}: references {req_id}, which was resolved as REJECTED; "
                f"rejected requirements are not executable"
            )
        if req.category not in categories:
            errors.append(
                f"{section}: references {req_id} with category {req.category}; "
                f"expected one of {sorted(categories)}"
            )

    for kpi in contract.kpis:
        check_ref(f"kpis.{kpi.name}", kpi.requirement_id, {"kpi"})
    for role in contract.roles:
        check_ref(f"roles.{role.name}", role.requirement_id, {"role"})
    for action in contract.allowed_actions:
        check_ref(
            f"allowed_actions.{action.action}",
            action.requirement_id,
            {"allowed_action", "security"},
        )
        unknown_roles = sorted(set(action.allowed_roles) - role_names)
        if unknown_roles:
            errors.append(
                f"allowed_actions.{action.action}: unknown roles {unknown_roles}"
            )
    for index, rule in enumerate(contract.escalation_rules):
        check_ref(
            f"escalation_rules[{index}]", rule.requirement_id, {"escalation", "security"}
        )
    for index, constraint in enumerate(contract.security_constraints):
        check_ref(
            f"security_constraints[{index}]",
            constraint.requirement_id,
            {"security", "data"},
        )
    if contract.slo.p95_latency_ms is not None:
        check_ref("slo.p95_latency_ms", contract.slo.p95_latency_ms.requirement_id, {"latency"})
    if contract.slo.cost_per_task_eur is not None:
        check_ref(
            "slo.cost_per_task_eur",
            contract.slo.cost_per_task_eur.requirement_id,
            {"cost"},
        )
    if contract.environment is not None:
        check_ref("environment", contract.environment.requirement_id, {"environment"})
    if contract.data_governance is not None:
        check_ref("data_governance", contract.data_governance.requirement_id, {"data"})

    # --- acceptance rules ---------------------------------------------------
    for req in requirements:
        if req.out_of_band_verification is None:
            continue
        if req.status != "resolved" or (
            req.resolution is not None and req.resolution.decision == "rejected"
        ):
            # the export presents these to the customer as commitments that
            # remain binding, so a rejected or still-contested requirement must
            # never carry one
            errors.append(
                f"{req.id}: carries out_of_band_verification but was not adopted "
                f"(status {req.status}, decision "
                f"{req.resolution.decision if req.resolution else 'none'}); only a "
                f"live promise can be verified out of band"
            )

    rule_ids: set[str] = set()
    slugs: set[str] = set()
    for rule in contract.acceptance_rules:
        if rule.id in rule_ids:
            errors.append(f"duplicate acceptance rule id {rule.id}")
        rule_ids.add(rule.id)
        if rule.slug in slugs:
            errors.append(f"duplicate acceptance rule slug {rule.slug!r}")
        slugs.add(rule.slug)

        req = by_id.get(rule.requirement_id)
        if req is None:
            errors.append(
                f"{rule.id}: enforces unknown requirement {rule.requirement_id}"
            )
        else:
            if req.status != "resolved":
                errors.append(
                    f"{rule.id}: enforces {req.id} whose status is {req.status}; "
                    f"only a resolved requirement can be tested"
                )
            if req.resolution is not None and req.resolution.decision == "rejected":
                errors.append(
                    f"{rule.id}: enforces {req.id}, which was resolved as REJECTED; "
                    f"a rejected requirement is not a promise to test"
                )
            if req.out_of_band_verification is not None:
                errors.append(
                    f"{rule.id}: enforces {req.id}, which is recorded as verified "
                    f"out of band; a requirement is either checked by the suite or "
                    f"checked elsewhere, never claimed as both"
                )
        for cited in rule.cites:
            if cited not in by_id:
                errors.append(f"{rule.id}: cites unknown requirement {cited}")

        named_actions = (
            [a.name for a in rule.expect.actions]
            + list(rule.expect.forbidden_actions)
            + list(rule.expect.max_action_calls)
        )
        for action in named_actions:
            if action not in action_names:
                errors.append(
                    f"{rule.id}: names action {action!r}, which is not in the "
                    f"contract's allowed_actions"
                )
        expected_names = [a.name for a in rule.expect.actions]
        repeated = sorted({n for n in expected_names if expected_names.count(n) > 1})
        if repeated:
            # the exported scenario carries one args_subset per action name, so a
            # repeated action would silently keep the last entry's arguments and
            # drop the others; the check the author wrote would vanish
            errors.append(
                f"{rule.id}: lists action(s) {repeated} more than once in expect."
                f"actions; one expectation per action, because arguments are "
                f"matched by action name"
            )
        if rule.expect.is_empty():
            # a scenario with no deterministic check cannot fail in replay mode,
            # which is the default and the only mode that runs without
            # credentials, so it would add a passing row having tested nothing.
            # A rubric does not close this: the judge does not run in replay.
            errors.append(
                f"{rule.id}: has no deterministic outcome (no expected or forbidden "
                f"actions, no call limits, no output constraints); a rubric alone "
                f"cannot fail in replay mode, so a rule that carries only one is "
                f"not a test"
            )
        if rule.type == "latency" and rule.max_steps is None:
            errors.append(
                f"{rule.id}: a latency rule needs max_steps, because the per-step "
                f"ceiling is what the SLO states"
            )

    # --- findings -----------------------------------------------------------
    report.conflict_groups = _conflict_groups(requirements)
    report.open_blocking_questions = [
        q.id for q in contract.open_questions if q.status == "open" and q.blocking
    ]
    report.open_nonblocking_questions = [
        q.id for q in contract.open_questions if q.status == "open" and not q.blocking
    ]
    if contract.slo.p95_latency_ms is None:
        report.pending_fields.append("slo.p95_latency_ms")
    if contract.slo.cost_per_task_eur is None:
        report.pending_fields.append("slo.cost_per_task_eur")
    if contract.environment is None:
        report.pending_fields.append("environment")
    if contract.data_governance is None:
        report.pending_fields.append("data_governance")

    # A contract is only executable through its requirements, so the reverse
    # direction matters as much as the forward one: a requirement that survived
    # review resolved and non-rejected, but that no executable section
    # references, is a customer promise that was accepted and then quietly
    # dropped out of the deployment. Requirements still in open conflict are
    # expected to be unwired (that is what resolving them decides), and a
    # rejected one must never be wired.
    report.unwired_requirements = [
        req.id
        for req in requirements
        if req.status == "resolved"
        and not (req.resolution is not None and req.resolution.decision == "rejected")
        and req.id not in referenced
    ]

    # Coverage of the acceptance suite, in the same spirit: a requirement that
    # describes agent behavior must either carry an acceptance rule or say, on
    # the record, that it cannot be checked by a trajectory and who checks it
    # instead. Silence is the failure mode; a promise nobody wrote a test for
    # and nobody excused is a promise the gate will pass without ever touching.
    ruled = contract.rules_by_requirement()
    report.untestable_requirements = [
        req.id
        for req in requirements
        if (req.category in BEHAVIORAL_CATEGORIES or req.id in behavioral_refs)
        and req.status == "resolved"
        and not (req.resolution is not None and req.resolution.decision == "rejected")
        and req.id not in ruled
        and req.out_of_band_verification is None
    ]

    # --- approval gate: no unreviewed contract can run -----------------------
    if contract.metadata.status == "approved":
        approval = report.approval_errors
        if not (contract.metadata.approved_by or "").strip():
            approval.append("approved contract has no approved_by")
        if not (contract.metadata.approved_at or "").strip():
            approval.append("approved contract has no approved_at")
        if contract.metadata.transcript_sha256 is None:
            approval.append(
                "approved contract does not pin transcript_sha256; "
                "approval requires byte-exact provenance"
            )
        if report.conflict_groups:
            approval.append(
                f"approved contract has open conflicts: {report.conflict_groups}"
            )
        if report.open_blocking_questions:
            approval.append(
                f"approved contract has blocking open questions: "
                f"{report.open_blocking_questions}"
            )
        if report.pending_fields:
            approval.append(
                f"approved contract has unfilled required fields: {report.pending_fields}"
            )
        if report.unwired_requirements:
            approval.append(
                f"approved contract adopts requirements that no executable section "
                f"references: {report.unwired_requirements}; an adopted promise must "
                f"be wired into the contract or resolved as rejected, never dropped"
            )
        if report.untestable_requirements:
            approval.append(
                f"approved contract has behavioral requirements with neither an "
                f"acceptance rule nor a recorded out-of-band verification: "
                f"{report.untestable_requirements}; the suite would pass without "
                f"ever checking them"
            )

    return report
