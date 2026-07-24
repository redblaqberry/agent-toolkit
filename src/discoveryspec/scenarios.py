"""Compile an approved deployment contract into agent-eval-gate scenarios.

The suite is a function of the contract. Every scenario is rendered from one
``acceptance_rules`` entry: a typed primitive that states the situation, the
message to send the agent under test, and the outcome that must be observable.
Nothing here knows what business this contract is for, which is the point;
earlier versions carried the ten invoice scenarios as templates bound to this
customer's requirement ids, so a second contract could not compile at all.

The five primitives are deliberately few:

- ``required_action``   the agent must take these actions
- ``forbidden_action``  the agent must never take these actions
- ``escalation``        under a stated condition, the agent must hand off
- ``latency``           exercises the interactive path the p95 SLO covers
- ``cost``              exercises the path the per-task cost ceiling covers

The output is agent-eval-gate's native scenario format (id, user_message,
checks, rubric, budgets, tags), so the gate runs it without modification;
DiscoverySpec does not implement another evaluator.

Fail-closed rules:
- Only an approved, structurally clean contract compiles ("no unreviewed
  contract can run"), and an approved contract with no acceptance rules is
  refused rather than exporting an empty suite that passes.
- Coverage is enforced by the validator: a requirement describing agent
  behavior must carry a rule or a recorded out-of-band verification, so a
  promise cannot pass the gate by never being tested. What the suite does not
  cover travels with the export, under ``not_covered``, instead of going quiet.
- Every action a rule names must exist in the contract's allowed_actions, and a
  rule with no observable outcome is an error, not a weak generic test.
- Scenario provenance is derived from the cited requirements' source_turns,
  never hand-written, so it cannot drift from contract provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import AcceptanceRule, DeploymentContract
from .transcript import Transcript
from .validate import validate_contract


class CompileError(ValueError):
    """The contract cannot be compiled into the acceptance suite.

    ``exit_code`` mirrors the CLI convention: 2 for structural problems,
    1 for approval-state problems (draft or dirty-approved contracts).
    ``problems`` carries the validator's own findings, so a refusal names what
    is wrong instead of reporting a count and making the operator go looking.
    """

    def __init__(
        self, message: str, exit_code: int = 2, problems: list[str] | None = None
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.problems = problems or []


@dataclass
class GateScenario:
    """One agent-eval-gate scenario plus its provenance.

    ``requirement_ids[0]`` is the enforced requirement; the rest are history.
    ``source_turns`` is filled by the compiler from the cited requirements.
    """

    id: str
    description: str
    user_message: str
    requirement_ids: list[str]
    manager_label: str = ""
    # whether this scenario exercises the interactive path the latency SLO
    # applies to; batch and approver-side flows are excluded from the p95
    # population, because the contract scopes the ceiling to interactive work
    interactive: bool = True
    source_turns: list[int] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    expected_tool_args: dict = field(default_factory=dict)
    expected_tools_ordered: bool = False
    forbidden_tools: list[str] = field(default_factory=list)
    max_tool_calls: dict = field(default_factory=dict)
    output_must_contain: list[str] = field(default_factory=list)
    output_must_not_contain: list[str] = field(default_factory=list)
    rubric: list[str] = field(default_factory=list)
    budgets: dict = field(default_factory=dict)

    def to_gate_dict(self) -> dict:
        """agent-eval-gate scenarios.yaml entry; provenance rides in tags."""
        checks: dict = {}
        if self.expected_tools:
            expected = []
            for name in self.expected_tools:
                entry: dict = {"name": name}
                if name in self.expected_tool_args:
                    entry["args_subset"] = dict(self.expected_tool_args[name])
                expected.append(entry)
            checks["expected_tools"] = expected
            if self.expected_tools_ordered:
                checks["expected_tools_ordered"] = True
        if self.forbidden_tools:
            checks["forbidden_tools"] = list(self.forbidden_tools)
        if self.max_tool_calls:
            checks["max_tool_calls"] = dict(self.max_tool_calls)
        if self.output_must_contain:
            checks["output_must_contain"] = list(self.output_must_contain)
        if self.output_must_not_contain:
            checks["output_must_not_contain"] = list(self.output_must_not_contain)

        entry: dict = {
            "id": self.id,
            "description": self.description,
            "user_message": self.user_message,
            "checks": checks,
            "tags": self.requirement_ids + [f"T{n:02d}" for n in self.source_turns],
        }
        if self.rubric:
            entry["rubric"] = list(self.rubric)
        if self.budgets:
            entry["budgets"] = dict(self.budgets)
        return entry


def _scenario_from_rule(
    rule: AcceptanceRule, contract: DeploymentContract
) -> GateScenario:
    """Render one typed rule into a gate scenario.

    Only the two measurement primitives read anything outside the rule: a
    latency rule turns the contract's per-step p95 into a whole-scenario budget,
    and a cost rule refuses to compile unless the ceiling it exercises exists.
    """
    budgets: dict = {}
    if rule.max_steps is not None:
        budgets["max_steps"] = rule.max_steps

    if rule.type == "latency":
        slo = contract.slo.p95_latency_ms
        # both refusals below are unreachable through any command, because the
        # validator rejects a latency rule without max_steps and an approved
        # contract with a null SLO; they stay as the local invariant of a
        # function that would otherwise produce a budget from None
        if slo is None:
            raise CompileError(
                f"{rule.id} exercises the latency ceiling, but the contract has no "
                f"slo.p95_latency_ms to measure against"
            )
        if rule.max_steps is None:
            raise CompileError(
                f"{rule.id} is a latency rule without max_steps, so there is no "
                f"step count to turn the per-step ceiling into a budget"
            )
        # the per-scenario budget is the stated per-step ceiling times the steps
        # this rule ALLOWS, so a trajectory that takes fewer, slower steps can
        # still sit inside it. It is a coarse bound on the scenario; the actual
        # per-step ceiling is enforced statistically across the whole run by
        # evaluate_slos, which is where a breach is caught.
        budgets["max_latency_s"] = (slo.value / 1000) * rule.max_steps
    if rule.type == "cost" and contract.slo.cost_per_task_eur is None:
        raise CompileError(
            f"{rule.id} exercises the per-task cost ceiling, but the contract has "
            f"no slo.cost_per_task_eur to measure against"
        )

    requirement_ids = [rule.requirement_id] + list(rule.cites)
    description = (
        f"Given {rule.given}, when {rule.when}, then {rule.then}. "
        f"[{', '.join(requirement_ids)}]"
    )
    return GateScenario(
        id=rule.slug,
        description=description,
        user_message=rule.message,
        requirement_ids=requirement_ids,
        manager_label=rule.label,
        interactive=rule.interactive,
        expected_tools=[a.name for a in rule.expect.actions],
        expected_tool_args={
            a.name: dict(a.args_subset) for a in rule.expect.actions if a.args_subset
        },
        expected_tools_ordered=rule.expect.actions_ordered,
        forbidden_tools=list(rule.expect.forbidden_actions),
        max_tool_calls=dict(rule.expect.max_action_calls),
        output_must_contain=list(rule.expect.output_contains),
        output_must_not_contain=list(rule.expect.output_excludes),
        rubric=list(rule.rubric),
        budgets=budgets,
    )


def compile_scenarios(
    contract: DeploymentContract, transcript: Transcript
) -> list[GateScenario]:
    """Build the acceptance suite from the contract's own acceptance rules."""
    report = validate_contract(contract, transcript)
    if report.exit_code != 0:
        raise CompileError(
            f"contract does not validate cleanly "
            f"(errors: {len(report.errors)}, approval: {len(report.approval_errors)})",
            exit_code=report.exit_code,
            problems=report.errors + report.approval_errors,
        )
    if contract.metadata.status != "approved":
        raise CompileError(
            "no unreviewed contract can run: status must be approved", exit_code=1
        )
    if not contract.acceptance_rules:
        # an empty suite passes everything, which is worse than no suite: it
        # produces a report that says the agent kept every promise
        raise CompileError(
            "approved contract carries no acceptance rules, so there is nothing to "
            "compile; an empty suite would report a clean pass having tested nothing",
            exit_code=1,
        )

    scenarios = [_scenario_from_rule(rule, contract) for rule in contract.acceptance_rules]
    _finish_compiled(scenarios, transcript, contract.requirement_by_id())
    return scenarios


def _finish_compiled(scenarios, transcript, by_id) -> None:
    """Derive scenario turn provenance and enforce compile-time invariants."""
    seen_ids: set[str] = set()
    turn_numbers = transcript.turn_numbers()
    for scenario in scenarios:
        if scenario.id in seen_ids:
            raise CompileError(f"duplicate scenario id {scenario.id}")
        seen_ids.add(scenario.id)

        # provenance is derived from the contract, never hand-written
        derived: list[int] = []
        for req_id in scenario.requirement_ids:
            for turn in by_id[req_id].source_turns:
                if turn not in derived:
                    derived.append(turn)
        if not derived:
            raise CompileError(
                f"{scenario.id}: cited requirements carry no transcript turns, so the "
                f"scenario would run without provenance"
            )
        missing_turns = sorted(set(derived) - turn_numbers)
        if missing_turns:
            raise CompileError(f"{scenario.id}: cites missing turns {missing_turns}")
        scenario.source_turns = derived


def gate_export(contract: DeploymentContract, transcript: Transcript) -> tuple[dict, dict]:
    """Build the scenarios.yaml payload and the gate-config.json payload."""
    scenarios = compile_scenarios(contract, transcript)
    scenarios_payload = {"scenarios": [s.to_gate_dict() for s in scenarios]}
    cost = contract.slo.cost_per_task_eur
    config_payload = {
        "source": {
            "project": contract.metadata.project,
            "system": contract.metadata.system,
            "customer": contract.metadata.customer,
            "transcript": contract.metadata.transcript,
            "transcript_sha256": contract.metadata.transcript_sha256,
            "approved_by": contract.metadata.approved_by,
            "approved_at": contract.metadata.approved_at,
        },
        "slo": {
            "p95_latency_ms": contract.slo.p95_latency_ms.value,
            "cost_per_task_eur": cost.value,
            "task_unit": cost.unit,
        },
        "scenario_provenance": {
            s.id: {
                "manager_label": s.manager_label,
                "interactive": s.interactive,
                "requirement_ids": s.requirement_ids,
                "source_turns": s.source_turns,
            }
            for s in scenarios
        },
        # what this suite does NOT check, carried with it on purpose: a promise
        # verified somewhere else is still a promise, and a reader of the export
        # should never have to infer the gap from an absence
        # only adopted promises: the report presents these as commitments that
        # remain binding, so a rejected or still-contested requirement must
        # never appear here. The validator refuses that combination outright;
        # this filter keeps the export honest even if it is reached another way.
        "not_covered": [
            {
                "requirement_id": req.id,
                "title": req.title,
                "stakeholder": req.stakeholder,
                "reason": req.out_of_band_verification.reason,
                "verified_by": req.out_of_band_verification.verified_by,
            }
            for req in contract.requirements
            if req.out_of_band_verification is not None
            and req.status == "resolved"
            and not (
                req.resolution is not None and req.resolution.decision == "rejected"
            )
        ],
    }
    return scenarios_payload, config_payload
