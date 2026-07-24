"""Run the approved contract's acceptance suite through agent-eval-gate.

``run`` closes the loop the contract promises: the suite executed is derived
from the approved contract at run time (never a stale export), agent-eval-gate
executes it with its own adapters and checks, and every verdict is linked back
to the requirement it enforces and the numbered customer statements that
requirement cites. A failing scenario is reported as a broken promise with the
stakeholder's words attached.

Fail-closed rules, mirroring agent-eval-gate's exit convention:
- harness errors (missing fixture, adapter crash) or judge errors are exit 2:
  the run is incomplete and proves nothing
- a failing scenario or a breached SLO is exit 1; there is no non-strict mode,
  because a deploy gate that shrugs at failures gates nothing
- the contract-level SLOs are enforced here, across the whole run: the p95
  latency ceiling is checked statistically over every interactive step in the
  run (nearest-rank, per step as the customer stated it), and the per-task
  cost ceiling is checked against every scenario's cost.
  A cost SLO that cannot be verified (no price table, unknown model) refuses
  the run instead of skipping the check; cache tokens are priced at the full
  input rate, which can only overstate cost, never hide a breach.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

from .models import DeploymentContract
from .transcript import Transcript


class RunError(ValueError):
    """The suite cannot be run or verified; exit 2 (infrastructure failure)."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = 2
        self.problems = problems or []


def require_gate() -> Any:
    """Import agent-eval-gate lazily; the run command is the only consumer."""
    try:
        import agent_eval_gate
    except ImportError as exc:
        raise RunError(
            "the run command executes scenarios through agent-eval-gate, which is "
            "not installed; install it from its repository, e.g. "
            "pip install -e ../agent-eval-gate"
        ) from exc
    return agent_eval_gate


def percentile_nearest_rank(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; with small n this is deliberately pessimistic
    (p95 of 10 samples is the maximum), which is the fail-closed direction
    for a latency ceiling."""
    if not values:
        raise RunError("cannot compute a percentile over an empty run")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def validate_price_table(prices: dict) -> None:
    """Refuse a price table with ANY malformed entry, used or not.

    Validating only the entries a trajectory happens to select would let a
    zero-rate or NaN entry sit in the table waiting for the run that picks it;
    a table is either wholly trustworthy or refused."""
    if not isinstance(prices, dict) or not prices:
        raise RunError("price table must be a non-empty JSON object of model rates")
    problems = []
    for model, table in prices.items():
        if not isinstance(table, dict):
            problems.append(f"{model!r}: entry is not an object")
            continue
        raw_rates = [table.get("input_per_mtok"), table.get("output_per_mtok")]
        # JSON numbers only: float() would also coerce booleans and numeric
        # strings, and neither is a monetary rate
        if any(
            isinstance(r, bool) or not isinstance(r, (int, float)) for r in raw_rates
        ):
            problems.append(
                f"{model!r}: input_per_mtok and output_per_mtok must be JSON "
                f"numbers, got {raw_rates!r}"
            )
            continue
        input_rate, output_rate = float(raw_rates[0]), float(raw_rates[1])
        if not all(math.isfinite(r) and r > 0 for r in (input_rate, output_rate)):
            problems.append(
                f"{model!r}: rates must be finite and strictly positive, got "
                f"input_per_mtok={input_rate!r}, output_per_mtok={output_rate!r}"
            )
    if problems:
        raise RunError(
            "price table is unusable; every entry must be valid before a cost "
            "SLO can be trusted",
            problems=problems,
        )


def scenario_cost_eur(trajectory: Any, prices: dict) -> float:
    """Cost of one scenario in EUR from a per-model price table.

    ``prices`` maps model id -> {"input_per_mtok": eur, "output_per_mtok": eur}.
    Cache creation and cache read tokens are priced at the full input rate:
    that overstates real cost, so the ceiling check can only be stricter than
    reality, never looser. An unknown model or malformed table refuses the
    run; a cost SLO that cannot be computed is never silently skipped.
    """
    table = prices.get(trajectory.model)
    if table is None:
        raise RunError(
            f"price table has no entry for model {trajectory.model!r}; "
            f"the per-task cost SLO cannot be verified"
        )
    raw_rates = [table.get("input_per_mtok"), table.get("output_per_mtok")]
    if any(isinstance(r, bool) or not isinstance(r, (int, float)) for r in raw_rates):
        raise RunError(
            f"price table entry for {trajectory.model!r} must carry numeric "
            f"input_per_mtok and output_per_mtok (JSON numbers, not booleans "
            f"or strings); got {raw_rates!r}"
        )
    input_rate, output_rate = float(raw_rates[0]), float(raw_rates[1])
    if not all(math.isfinite(rate) and rate > 0 for rate in (input_rate, output_rate)):
        # a negative rate makes costs negative (false PASS), a zero rate lets an
        # arbitrarily expensive agent pass the ceiling, and NaN surfaces as a
        # quality failure; a real price is finite and strictly positive
        raise RunError(
            f"price table entry for {trajectory.model!r} must carry finite, "
            f"strictly positive rates (a zero rate would let any cost pass the "
            f"ceiling); got input_per_mtok={input_rate!r}, "
            f"output_per_mtok={output_rate!r}"
        )
    usage = trajectory.total_usage
    counters = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }
    negative = {name: value for name, value in counters.items() if value < 0}
    if negative:
        raise RunError(
            f"trajectory for {trajectory.scenario_id!r} carries negative usage "
            f"counters {negative}; a malformed recording cannot verify the cost SLO"
        )
    input_like = (
        usage.input_tokens
        + usage.cache_creation_input_tokens
        + usage.cache_read_input_tokens
    )
    return (input_like * input_rate + usage.output_tokens * output_rate) / 1_000_000


def execute_scenarios(
    scenarios: list,
    adapter: Any,
    use_judge: bool = False,
    judge_model: Optional[str] = None,
    echo: Optional[Callable[[str], None]] = None,
) -> tuple[list, list[str]]:
    """Run every scenario through ``adapter`` and agent-eval-gate's checks.

    Returns (results, judge_error_scenario_ids). An adapter crash becomes an
    errored trajectory (the scenario fails and the run exits 2); a judge
    outage fails that scenario's criteria instead of losing the run, exactly
    as agent-eval-gate's own runner behaves.
    """
    gate = require_gate()
    results: list = []
    judge_errors: list[str] = []
    for scenario in scenarios:
        if echo is not None:
            echo(f"running {scenario.id} ...")
        try:
            trajectory = adapter.run(scenario)
        except Exception as exc:  # corrupt fixture etc.: fail the scenario, keep the run
            trajectory = gate.Trajectory(
                scenario_id=scenario.id, error=f"{type(exc).__name__}: {exc}"
            )
        result = gate.ScenarioResult(
            scenario_id=scenario.id,
            trajectory=trajectory,
            checks=gate.run_checks(scenario, trajectory),
        )
        if use_judge and scenario.rubric and trajectory.error is None:
            try:
                result.judge = gate.judge_trajectory(
                    scenario, trajectory, model=judge_model
                )
            except Exception as exc:  # judge outage fails the criteria, not the run data
                judge_errors.append(scenario.id)
                result.judge = gate.JudgeVerdict(
                    model=judge_model or "",
                    criteria=[
                        gate.CriterionVerdict(
                            criterion=criterion,
                            passed=False,
                            reasoning=f"judge call failed: {type(exc).__name__}: {exc}",
                        )
                        for criterion in scenario.rubric
                    ],
                )
        results.append(result)
    return results, judge_errors


def evaluate_slos(
    results: list,
    costs_eur: dict[str, float],
    slo: dict,
    interactive_ids: Optional[set[str]] = None,
) -> dict:
    """Check the contract-level SLOs across the whole run.

    The latency SLO is per interactive STEP, matching the customer's words
    ("p95 under 2 seconds per step", T30), and its population is scoped to
    the scenarios their acceptance rule marks interactive: contracts scope the
    ceiling to the interactive path, so a slow batch or approver-side flow must
    not fail an otherwise compliant SLO. ``interactive_ids=None`` samples every
    scenario.

    A negative or non-finite recorded latency is a malformed fixture and
    refuses the run. An SLO with no samples at all (every relevant scenario
    errored) is reported as unmeasured and NOT passed, so the run report can
    still be written and carry its INCOMPLETE verdict.
    """
    latencies_ms = []
    for result in results:
        if interactive_ids is not None and result.scenario_id not in interactive_ids:
            continue
        for step in result.trajectory.steps:
            if not math.isfinite(step.latency_s) or step.latency_s < 0:
                raise RunError(
                    f"trajectory for {result.scenario_id!r} carries an invalid "
                    f"step latency {step.latency_s!r}; a malformed recording "
                    f"cannot verify the latency SLO"
                )
            latencies_ms.append(step.latency_s * 1000)
    latency_limit = slo["p95_latency_ms"]
    if latencies_ms:
        observed_p95 = round(percentile_nearest_rank(latencies_ms, 95), 1)
        latency_passed = observed_p95 <= latency_limit
    else:
        observed_p95 = None
        latency_passed = False  # unmeasured is never a pass
    cost_limit = slo["cost_per_task_eur"]
    max_cost = max(costs_eur.values()) if costs_eur else None
    return {
        "p95_latency_ms": {
            "limit": latency_limit,
            "observed": observed_p95,
            "passed": latency_passed,
        },
        "cost_per_task_eur": {
            "limit": cost_limit,
            # the ceiling is per unit of work, so the number never travels
            # without the noun it is per
            "unit": slo.get("task_unit", "task"),
            "max_observed": round(max_cost, 6) if max_cost is not None else None,
            "passed": max_cost is not None and max_cost <= cost_limit,
        },
    }


def slos_passed(slo_verdict: dict) -> bool:
    return all(entry["passed"] for entry in slo_verdict.values())


def run_exit_code(results: list, judge_errors: list[str], slo_verdict: dict) -> int:
    if any(r.trajectory.error is not None for r in results) or judge_errors:
        return 2
    if any(not r.passed for r in results) or not slos_passed(slo_verdict):
        return 1
    return 0


def provenance_report(
    contract: DeploymentContract,
    transcript: Transcript,
    config_payload: dict,
    results: list,
    costs_eur: dict[str, float],
    slo_verdict: dict,
    mode: str,
    agent_model: str,
    run_id: str = "",
    generated_at: str = "",
    contract_sha256: str = "",
    judge_error_ids: Optional[list[str]] = None,
) -> dict:
    """The run report DiscoverySpec exists for: every verdict linked back to
    the requirement it enforces and the customer statements behind it.

    ``contract_sha256`` binds the report to the exact contract file bytes the
    run executed, so downstream rendering can refuse a mismatched pairing.
    The verdict is three-valued: PASS, FAIL, or INCOMPLETE when any scenario
    could not be executed; an incomplete run proves nothing and must never
    read as a mere acceptance failure."""
    by_id = contract.requirement_by_id()
    provenance = config_payload["scenario_provenance"]
    judge_failed = set(judge_error_ids or [])
    entries = []
    for result in results:
        prov = provenance[result.scenario_id]
        enforced_id = prov["requirement_ids"][0]
        enforced = by_id[enforced_id]
        citations = [
            {
                "turn": number,
                "speaker": transcript.turn(number).speaker,
                "role": transcript.turn(number).role,
                "text": transcript.turn(number).text,
            }
            for number in prov["source_turns"]
        ]
        failed_checks = [
            {"name": check.name, "detail": check.detail}
            for check in result.checks
            if not check.passed
        ]
        failed_criteria = [
            {"criterion": c.criterion, "reasoning": c.reasoning}
            for c in (result.judge.criteria if result.judge else [])
            if not c.passed
        ]
        entries.append(
            {
                "scenario_id": result.scenario_id,
                "manager_label": prov.get("manager_label") or result.scenario_id,
                "passed": result.passed,
                "score": round(result.score, 3),
                "enforces": {
                    "requirement_id": enforced_id,
                    "title": enforced.title,
                    "statement": enforced.statement,
                    "stakeholder": enforced.stakeholder,
                },
                "cited_requirements": list(prov["requirement_ids"]),
                "source_turns": list(prov["source_turns"]),
                "citations": citations,
                "failed_checks": failed_checks,
                "failed_criteria": failed_criteria,
                "error": results_error(result),
                "judge_error": result.scenario_id in judge_failed,
                "latency_ms": round(result.trajectory.total_latency_s * 1000, 1),
                "cost_eur": costs_eur.get(result.scenario_id),
            }
        )
    passed_count = sum(1 for r in results if r.passed)
    # a judge outage is infrastructure, not a broken promise: it makes the
    # run INCOMPLETE exactly like a missing trajectory does
    incomplete = (
        any(r.trajectory.error is not None for r in results)
        or bool(judge_error_ids)
    )
    if incomplete:
        verdict = "INCOMPLETE"
    elif passed_count == len(results) and slos_passed(slo_verdict):
        verdict = "PASS"
    else:
        verdict = "FAIL"
    source = dict(config_payload["source"])
    if contract_sha256:
        source["contract_sha256"] = contract_sha256
    return {
        "source": source,
        "run_id": run_id,
        "generated_at": generated_at,
        "mode": mode,
        "agent_model": agent_model,
        "scenarios": entries,
        "passed": passed_count,
        "total": len(results),
        "slo": slo_verdict,
        # promises this suite deliberately does not check, carried into the run
        # record so a clean verdict can never be read as broader than it is
        "not_covered": list(config_payload.get("not_covered", [])),
        "verdict": verdict,
    }


def results_error(result: Any) -> Optional[str]:
    return result.trajectory.error


def console_lines(report: dict) -> list[str]:
    """Human-readable run summary; failures quote the promise they break."""
    lines = [
        f"run: {report['source']['project']}  mode={report['mode']}  "
        f"agent={report['agent_model']}",
        "-" * 78,
    ]
    for entry in report["scenarios"]:
        enforced = entry["enforces"]
        turns = ", ".join(f"T{n:02d}" for n in entry["source_turns"])
        status = "PASS" if entry["passed"] else "FAIL"
        lines.append(
            f"[{status}] {entry['scenario_id']:<34} enforces "
            f"{enforced['requirement_id']} [{turns}]"
        )
        if not entry["passed"]:
            for check in entry["failed_checks"]:
                detail = f": {check['detail']}" if check["detail"] else ""
                lines.append(f"        x {check['name']}{detail}")
            for criterion in entry["failed_criteria"]:
                lines.append(f"        x judge: {criterion['criterion']}")
            if entry["error"]:
                lines.append(f"        ! harness error: {entry['error']}")
            first_citation = entry["citations"][0] if entry["citations"] else None
            if first_citation is not None:
                lines.append(
                    f"        broken promise, {enforced['stakeholder']} at "
                    f"T{first_citation['turn']:02d}: \"{enforced['statement']}\""
                )
    slo = report["slo"]
    latency = slo["p95_latency_ms"]
    cost = slo["cost_per_task_eur"]
    latency_observed = (
        f"{latency['observed']} ms" if latency["observed"] is not None
        else "not measured"
    )
    cost_observed = (
        f"EUR {cost['max_observed']}" if cost["max_observed"] is not None
        else "not measured"
    )
    lines += [
        "-" * 78,
        f"SLO p95 latency (interactive steps): {latency_observed} observed / "
        f"{latency['limit']} ms ceiling -> {'PASS' if latency['passed'] else 'FAIL'}",
        f"SLO cost per {cost.get('unit', 'task')}: {cost_observed} max observed / "
        f"EUR {cost['limit']} ceiling -> {'PASS' if cost['passed'] else 'FAIL'}",
        f"verdict: {report['verdict']} "
        f"({report['passed']}/{report['total']} scenarios)",
    ]
    for entry in report.get("not_covered", []):
        lines.append(
            f"not covered by this suite: {entry['requirement_id']} "
            f"({entry['title']}), verified by {entry['verified_by']}"
        )
    return lines
