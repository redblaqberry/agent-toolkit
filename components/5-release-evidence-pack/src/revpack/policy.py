"""The release policy engine, and the floor a policy cannot dig under.

Rule kinds are a closed enum implemented here in Python. The YAML selects and
parameterizes them; it cannot express new semantics. There is no expression
language and nothing is ``eval``ed, so reviewing a policy means reading a short
list of declared intents rather than auditing arbitrary code.

The floor exists because a purely declarative policy is trivially defeatable: a
policy with no required evidence and no rules satisfies "nothing failed" and
clears every release. A safety property that a config file can switch off is
decoration. The floor lives in code and ``validate_policy`` refuses any policy
that weakens it.
"""

from __future__ import annotations

import math

from .errors import PolicyError
from .lattice import Status, reduce_statuses, worst
from .models import (
    ClassOutcome,
    Policy,
    ReleaseDescriptor,
    Rule,
    RuleOutcome,
    TraceabilityMap,
)
from .parsers import Reading

# ---------------------------------------------------------------------------
# the floor
# ---------------------------------------------------------------------------

FLOOR_REQUIRED_KINDS = ("contract", "gate-run")
# requirement_coverage joins the floor because without it a policy can satisfy
# every other check while never relating a single piece of evidence to a single
# contract requirement. "Nothing failed" is not the same as "the thing we
# promised was tested".
FLOOR_BLOCKING_RULES = ("release_binding", "assertion_density", "requirement_coverage")

def _must_signature_present(req, readings: list[Reading]) -> str | None:
    """Name the evidence of this kind that carries no producer attestation block.

    Deliberately named for what it checks. Cryptographically verifying a
    producer's own attestation would need that producer's public key as a
    separate pack input, which v0.1 does not take. Calling this
    `signature_verified` would claim an assurance the code does not provide,
    which is the exact failure this project exists to catch.
    """
    unsigned = [
        r.evidence_id
        for r in readings
        if r.kind == req.kind and not r.facts.get("signature_present", False)
    ]
    return f"carries no signature block: {', '.join(unsigned)}" if unsigned else None


# Every `must` token and the check it runs, in one table.
_MUST_CHECKS = {"signature_present": _must_signature_present}

# Closed vocabulary for `must`, derived from the table above rather than written
# out beside it, so a token cannot be accepted by validation without something
# evaluating it. An unrecognized token used to validate happily and check
# nothing, so a typo like `signature_verified` silently disabled the requirement
# it was written to express. `approved` was the same hole wearing a spelling
# nobody would query: it was accepted here, implemented nowhere, and told a
# policy author their requirement was in force while it decided nothing. A token
# that is accepted and ignored is worse than one that is rejected, because the
# rejection is the only version an author can see.
MUST_TOKENS = frozenset(_MUST_CHECKS)

SEVERITY_RANK = {"info": 0, "risky": 1, "breaking": 2}


def validate_policy(policy: Policy) -> None:
    """Raise ``PolicyError`` unless the policy meets the floor."""
    problems: list[str] = []

    declared_kinds = {req.kind for req in policy.required_evidence}
    for kind in FLOOR_REQUIRED_KINDS:
        if kind not in declared_kinds:
            problems.append(f"policy floor: evidence kind {kind!r} is always required")

    for req in policy.required_evidence:
        if req.kind in FLOOR_REQUIRED_KINDS and req.min_count < 1:
            problems.append(
                f"policy floor: {req.kind!r} requires min_count >= 1, got {req.min_count}"
            )
        unknown = sorted(set(req.must) - MUST_TOKENS)
        if unknown:
            problems.append(
                f"evidence kind {req.kind!r}: unknown must token(s) {unknown}; expected a "
                f"subset of {sorted(MUST_TOKENS)}"
            )

    # Grouped, not keyed by last-wins. Keying by kind made validation
    # order-dependent: the same pair of rules passed or failed depending on
    # which came second.
    by_kind: dict[str, list[Rule]] = {}
    for rule in policy.rules:
        by_kind.setdefault(rule.kind, []).append(rule)

    for kind in FLOOR_BLOCKING_RULES:
        rules = by_kind.get(kind, [])
        if not rules:
            problems.append(f"policy floor: rule kind {kind!r} is always required")
        for rule in rules:
            if not rule.blocking:
                problems.append(
                    f"policy floor: rule {rule.id!r} ({kind}) must be blocking; a policy "
                    "cannot make it advisory"
                )

    for rule in by_kind.get("assertion_density", []):
        if rule.min_behavioral_checks < 1:
            problems.append(
                f"policy floor: rule {rule.id!r} requires min_behavioral_checks >= 1, so a "
                "scenario that asserts nothing can never count as evidence"
            )

    seen: set[str] = set()
    for rule in policy.rules:
        if rule.id in seen:
            problems.append(f"duplicate rule id {rule.id!r}")
        seen.add(rule.id)

    if problems:
        raise PolicyError("policy is invalid", problems=problems)


# ---------------------------------------------------------------------------
# evidence classes
# ---------------------------------------------------------------------------


def _cover_targets(projection: str | None, trace: TraceabilityMap) -> list[str]:
    """Resolve a coverage projection to concrete scenario ids.

    Projections are a closed set with defined meanings. Coverage is measured
    against what a human declared in the traceability map, never inferred, so a
    thin map produces a visible gap rather than a silently satisfied class.
    """
    if projection is None:
        return []
    if projection == "traceability_scenarios":
        return sorted({t for e in trace.entries for t in e.silobench_tasks})
    if projection == "traceability_statediff_scenarios":
        return sorted({s for e in trace.entries for s in e.statediff_scenarios})
    if projection == "all_declared":
        return sorted(
            {t for e in trace.entries for t in e.silobench_tasks}
            | {s for e in trace.entries for s in e.statediff_scenarios}
            | {g for e in trace.entries for g in e.gate_scenarios}
        )
    raise PolicyError(f"unknown must_cover projection {projection!r}")


def _executing_ids(readings: list[Reading]) -> dict[str, set[str]]:
    """The ids that actually appear in the collected evidence, by namespace.

    Kept separate per producer rather than unioned. A traceability entry names a
    SiloBench task, a StateDiff scenario, and a gate scenario in three different
    fields, and collapsing them would let a StateDiff scenario id satisfy a
    declared gate scenario that was never run.

    SiloBench task ids are resolvable from three places on purpose: a verify
    entry runs the task, the gate's scenario ids are the same task set (pinned
    against real output in the conformance suite), and a StateDiff verdict
    records which SiloBench task it graded. Each of those is a real artifact
    naming that task; none of them is the traceability map naming itself.
    """
    silobench: set[str] = set()
    statediff: set[str] = set()
    gate: set[str] = set()
    for reading in readings:
        if reading.kind == "gate-run":
            names = set((reading.facts.get("scenarios") or {}).keys())
            gate |= names
            silobench |= names
        elif reading.kind == "state-verdict":
            scenario = reading.facts.get("scenario_id")
            task = reading.facts.get("silobench_task")
            if scenario:
                statediff.add(scenario)
            if task:
                silobench.add(task)
        elif reading.kind == "silobench-verify":
            silobench |= set((reading.facts.get("tasks") or {}).keys())
    return {"silobench": silobench, "statediff": statediff, "gate": gate}


def _unresolved_entry_ids(entry, executing: dict[str, set[str]]) -> list[str]:
    """The ids an entry names that appear in no collected evidence."""
    return sorted(
        [t for t in entry.silobench_tasks if t not in executing["silobench"]]
        + [s for s in entry.statediff_scenarios if s not in executing["statediff"]]
        + [g for g in entry.gate_scenarios if g not in executing["gate"]]
    )


def _named_entry_ids(entry) -> list[str]:
    return [*entry.silobench_tasks, *entry.statediff_scenarios, *entry.gate_scenarios]


def covered_requirements(readings: list[Reading], trace: TraceabilityMap) -> set[str]:
    """The requirements some collected artifact actually speaks about.

    One definition of "covered", called by the blocking coverage rule and by the
    traceability summary written into ``decision.json``. They used to compute it
    separately and disagree: the summary counted any requirement a map entry
    mentioned, so a pack could list a requirement under ``covered_requirements``
    while the rule beside it reported that same requirement has no executing
    evidence. Two answers to one question in one signed document is worse than
    either answer alone, because the reader picks, and the summary is the half
    that gets skimmed.

    Naming a task is not evidence that the task ran. The map is a human
    declaration, so an entry is only coverage when at least one id it names lands
    on an artifact in this pack.
    """
    executing = _executing_ids(readings)
    covered: set[str] = set()
    for entry in trace.entries:
        named = _named_entry_ids(entry)
        if named and len(_unresolved_entry_ids(entry, executing)) < len(named):
            covered.add(entry.requirement)
    return covered


def _covered_ids(readings: list[Reading]) -> set[str]:
    """Every id the given artifacts speak about, flat.

    Not a duplicate of ``_executing_ids``: its callers have already narrowed
    ``readings`` to one evidence kind, so the namespace is settled by the caller
    and a flat set is the right answer. ``_executing_ids`` is handed the whole
    pack and has to keep the namespaces apart itself.
    """
    covered: set[str] = set()
    for reading in readings:
        if reading.kind == "gate-run":
            covered |= set((reading.facts.get("scenarios") or {}).keys())
        elif reading.kind == "state-verdict":
            scenario = reading.facts.get("scenario_id")
            task = reading.facts.get("silobench_task")
            if scenario:
                covered.add(scenario)
            if task:
                covered.add(task)
        elif reading.kind == "silobench-verify":
            covered |= set((reading.facts.get("tasks") or {}).keys())
    return covered


def evaluate_classes(
    policy: Policy, readings: list[Reading], trace: TraceabilityMap
) -> list[ClassOutcome]:
    outcomes: list[ClassOutcome] = []
    for req in policy.required_evidence:
        members = [(r.evidence_id, r.status) for r in readings if r.kind == req.kind]

        if len(members) < req.min_count:
            outcomes.append(
                ClassOutcome(
                    kind=req.kind,
                    status=Status.INCONCLUSIVE,
                    count=len(members),
                    min_count=req.min_count,
                    reduce=req.reduce,
                    members=[eid for eid, _ in members],
                    detail=(
                        f"{len(members)} artifact(s) of kind {req.kind!r}, policy requires "
                        f"{req.min_count}"
                    ),
                )
            )
            continue

        status = reduce_statuses(members, req.reduce, req.authoritative_evidence_id)
        detail = f"{len(members)} artifact(s) reduced by {req.reduce}"
        uncovered: list[str] = []

        if req.must_cover:
            targets = _cover_targets(req.must_cover, trace)
            members_of_kind = [r for r in readings if r.kind == req.kind]
            if req.reduce == "authoritative":
                # Under `authoritative` the policy named one artifact as the one
                # that decides and demoted the rest to informational. Unioning
                # scenario ids across every reading of the kind let exactly
                # those demoted artifacts supply the coverage that cleared the
                # class, so an artifact the policy explicitly said does not
                # decide could still decide the coverage half of the question.
                covering = [
                    r for r in members_of_kind
                    if r.evidence_id == req.authoritative_evidence_id
                ]
            else:
                covering = members_of_kind
            covered = _covered_ids(covering)
            uncovered = sorted(t for t in targets if t not in covered)
            if not targets:
                # An empty projection satisfies a coverage requirement without
                # checking anything. A policy that asks for coverage against a
                # map that names nothing has not been satisfied, it has been
                # bypassed.
                status = worst([status, Status.INCONCLUSIVE])
                detail += (
                    f"; must_cover {req.must_cover!r} resolves to no targets, so the "
                    "coverage requirement is vacuous"
                )
            elif uncovered:
                # A class satisfied by one artifact of the right kind while the
                # scenarios the release actually depends on have no evidence is
                # the coverage-by-count hole. Missing coverage is unknown, not
                # proven failure.
                status = worst([status, Status.INCONCLUSIVE])
                detail += f"; {len(uncovered)} declared target(s) have no evidence"

        for token in sorted(set(req.must)):
            # Looked up rather than branched on, so every token the policy is
            # allowed to declare reaches an implementation. `validate_policy` has
            # already refused anything outside the table, and a KeyError here
            # would mean the vocabulary and the checks had drifted apart, which
            # is the state that let a declared requirement evaluate to nothing.
            note = _MUST_CHECKS[token](req, readings)
            if note:
                status = worst([status, Status.INCONCLUSIVE])
                detail += f"; {note}"

        outcomes.append(
            ClassOutcome(
                kind=req.kind,
                status=status,
                count=len(members),
                min_count=req.min_count,
                reduce=req.reduce,
                members=[eid for eid, _ in members],
                uncovered=uncovered,
                detail=detail,
            )
        )
    return outcomes


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def _rule_requirement_coverage(
    rule: Rule, readings: list[Reading], trace: TraceabilityMap, **_: object
) -> RuleOutcome:
    contract = next((r for r in readings if r.kind == "contract"), None)
    if contract is None:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail="no contract evidence, so requirement coverage cannot be measured",
        )
    declared = set(contract.facts.get("requirement_ids") or [])
    if not declared:
        # Vacuous truth is the enemy here. "All zero requirements are covered"
        # is technically true and proves nothing, and a contract with no
        # requirements is degenerate anyway: DiscoverySpec's own schema requires
        # at least one. Absence of anything to check is absence of proof.
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail=(
                "the contract declares no requirements, so coverage is vacuous and "
                "nothing about this release has been proven"
            ),
        )

    # An entry that names no task and no scenario points at no executing
    # evidence. Counting it as coverage would let a map of bare requirement ids
    # report full traceability while nothing was ever run.
    #
    # Naming one is not enough either. The ids are resolved against the evidence
    # actually collected, because an entry pointing at a task that is not in the
    # pack proves nothing about the requirement it claims to cover: the map is a
    # human declaration, and a declaration that resolves to no artifact is the
    # same empty claim as a declaration that names nothing at all. Only a
    # traceability id that lands on collected evidence establishes coverage.
    executing = _executing_ids(readings)
    unresolved_by_requirement: dict[str, list[str]] = {}
    empty_entries: list[str] = []
    for entry in trace.entries:
        named = _named_entry_ids(entry)
        if not named:
            empty_entries.append(entry.requirement)
            continue
        unresolved = _unresolved_entry_ids(entry, executing)
        if unresolved:
            unresolved_by_requirement.setdefault(entry.requirement, []).extend(unresolved)

    # The same function the decision's traceability summary calls, so this rule
    # and that summary cannot report different requirements as covered.
    mapped = covered_requirements(readings, trace)
    uncovered = sorted(declared - mapped)
    dangling = sorted({e.requirement for e in trace.entries} - declared)
    # Only the ids belonging to requirements that are covered anyway: an
    # uncovered requirement is already reported as uncovered, and repeating its
    # ids here would double-count the same gap.
    unresolved_findings = sorted(
        f"{rid}: names {ids} which appear in no collected evidence"
        for rid, ids in unresolved_by_requirement.items()
        if rid in mapped
    )

    findings = [f"{rid}: no traceability entry naming executing evidence" for rid in uncovered]
    findings += [f"{rid}: mapped but absent from the contract" for rid in dangling]
    findings += [f"{rid}: traceability entry names no task or scenario" for rid in empty_entries]
    findings += unresolved_findings

    if dangling:
        status = Status.FAIL
        detail = "traceability map references requirements the contract does not contain"
    elif uncovered:
        status = Status.FAIL
        detail = f"{len(uncovered)} of {len(declared)} requirements have no executing evidence"
    elif unresolved_findings:
        # Part of the declared evidence for a covered requirement is not in this
        # pack. That is a gap in knowledge rather than a proven violation, so it
        # is inconclusive, and inconclusive still cannot clear a release.
        status = Status.INCONCLUSIVE
        detail = (
            f"{len(unresolved_findings)} requirement(s) are mapped partly to evidence this "
            "pack does not contain"
        )
    else:
        status = Status.PASS
        detail = f"all {len(declared)} requirements are mapped to collected evidence"
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=status,
        detail=detail, findings=findings,
    )


def _rule_assertion_density(rule: Rule, readings: list[Reading], **_: object) -> RuleOutcome:
    findings: list[str] = []
    for reading in readings:
        if reading.kind != "gate-run":
            continue
        for scenario_id, info in (reading.facts.get("scenarios") or {}).items():
            if info.get("behavioral_checks", 0) < rule.min_behavioral_checks:
                findings.append(
                    f"{scenario_id}: {info.get('behavioral_checks', 0)} behavioral check(s) of "
                    f"{info.get('total_checks', 0)} total, below the required "
                    f"{rule.min_behavioral_checks}"
                )
    if findings:
        # Asserting nothing is an absence of proof, not proof of a defect, so
        # this is inconclusive rather than a failure. It still cannot clear a
        # release.
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail=f"{len(findings)} scenario(s) score as passing while asserting nothing",
            findings=findings,
        )
    inspected = sum(len(r.facts.get("scenarios") or {}) for r in readings if r.kind == "gate-run")
    if not inspected:
        # Passing after inspecting nothing is the same vacuity this rule exists
        # to catch, one level up.
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail="no gate scenarios were inspected, so assertion density is unmeasured",
        )
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.PASS,
        detail=f"all {inspected} scenario(s) carry at least one behavioral assertion",
    )


def _metric_value(raw: object) -> float | None:
    """A usable metric reading, or None when the artifact does not carry one.

    Two ways a value gets past a ceiling without ever being compared to it.
    ``json.loads`` accepts ``NaN``, and every comparison against NaN is False,
    so ``NaN > limit`` is False and the code falls through to the branch that
    reports a blocking SLO as satisfied. A string or a null never reaches a
    comparison at all and would abort the aggregation with a TypeError instead.

    ``bool`` is excluded deliberately: it subclasses ``int``, so ``True`` would
    otherwise be read as one millisecond. Negative values are refused for the
    same reason a NaN is: no run took less than no time and no invocation cost
    less than nothing, so a producer emitting one is reporting something that is
    not the measurement.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _ceiling(slo: dict, key: str) -> tuple[float | None, bool]:
    """The declared ceiling for ``key`` as ``(value, declared)``.

    The flag separates "the contract sets no ceiling here", which is silence and
    means this rule stays out of it, from "it sets one that cannot be read as a
    number", which is a malformed contract. Collapsing the second into the first
    would turn an unreadable ceiling into an absent one, and an absent ceiling
    is never enforced.
    """
    # `slo.get(key)` collapses a MISSING key and an explicit `key: null` to the
    # same None, so an equality check on the value alone read a declared-but-null
    # ceiling as absent, and an absent ceiling is never enforced. Membership is
    # the real question: only a key that is not present at all is silence. Every
    # value the key holds, null included, is a ceiling the contract set, and one
    # that cannot be read as a number is reported unevidenced rather than absent.
    if key not in slo:
        return None, False
    raw = slo[key]
    if not isinstance(raw, dict):
        return None, True
    return _metric_value(raw.get("value")), True


def _slo_latency(slo: dict, gate: Reading) -> tuple[list[Status], list[str]]:
    limit_ms, declared = _ceiling(slo, "p95_latency_ms")
    if not declared:
        return [], []
    if limit_ms is None:
        return [Status.INCONCLUSIVE], [
            f"{gate.evidence_id} latency: the contract declares a p95 ceiling that is not a "
            "usable number, so nothing can be measured against it"
        ]
    if gate.facts.get("mode") == "replay":
        # Replay copies the fixture trajectory verbatim, so any latency in it is
        # whenever the fixture was recorded. Nothing in run.json dates a
        # fixture, so freshness cannot be established from the artifact.
        return [Status.INCONCLUSIVE], [
            f"{gate.evidence_id} latency: replay run, so recorded latencies are historical "
            "fixture values and cannot evidence this release"
        ]

    scenarios = gate.facts.get("scenarios") or {}
    observed_ms: list[float] = []
    unusable: list[str] = []
    for scenario_id, info in scenarios.items():
        value = _metric_value(info.get("latency_s"))
        if value is None:
            unusable.append(str(scenario_id))
        else:
            observed_ms.append(value * 1000)

    statuses: list[Status] = []
    findings: list[str] = []
    if observed_ms and max(observed_ms) > limit_ms:
        statuses.append(Status.FAIL)
        # The worst observed value, used as a conservative stand-in for a p95:
        # run.json records per-step latency but no per-request distribution, so
        # a true percentile is not computable from it. Erring strict is the safe
        # direction for a release gate.
        findings.append(
            f"{gate.evidence_id} latency: worst observed {max(observed_ms):.0f} ms exceeds "
            f"the {limit_ms} ms p95 ceiling (worst observed is used as a conservative "
            "proxy; the artifact carries no request distribution)"
        )
    if unusable:
        # A scenario whose latency is NaN, negative, or not a number has not
        # been measured. Leaving it in the comparison is how it reaches the
        # satisfied branch of a blocking ceiling without being compared.
        statuses.append(Status.INCONCLUSIVE)
        findings.append(
            f"{gate.evidence_id} latency: {len(unusable)} scenario(s) record no usable "
            f"latency ({', '.join(sorted(unusable)[:5])}), so the ceiling is unevidenced "
            "for them"
        )
    elif not observed_ms:
        statuses.append(Status.INCONCLUSIVE)
        findings.append(f"{gate.evidence_id} latency: no scenarios to measure")
    elif not statuses:
        statuses.append(Status.PASS)
    return statuses, findings


def _slo_cost(
    slo: dict, gate: Reading, release: ReleaseDescriptor | None
) -> tuple[list[Status], list[str]]:
    limit_eur, declared = _ceiling(slo, "cost_per_invoice_eur")
    if not declared:
        return [], []
    if limit_eur is None:
        return [Status.INCONCLUSIVE], [
            f"{gate.evidence_id} cost: the contract declares a ceiling that is not a usable "
            "number, so nothing can be measured against it"
        ]

    scenarios = gate.facts.get("scenarios") or {}
    costs: list[float] = []
    unpriced: list[str] = []
    unusable: list[str] = []
    for scenario_id, info in scenarios.items():
        raw = info.get("cost_usd")
        if raw is None:
            # A scenario the run put no price against. Skipping it measured the
            # priced subset and offered the result as the run's worst cost, so a
            # run where one scenario was priced under the ceiling and another was
            # not priced at all satisfied a blocking cost ceiling on the strength
            # of the scenarios that answered. That is the same claim the latency
            # rule refuses to make, and it is refused here for the same reason:
            # an unpriced invocation is not a cheap one.
            unpriced.append(str(scenario_id))
            continue
        value = _metric_value(raw)
        if value is None:
            unusable.append(str(scenario_id))
        else:
            costs.append(value)

    rate = release.eur_per_usd if release is not None else None
    if not costs and not unusable:
        # Nothing carried a number at all, which is what a run executed without a
        # price table looks like. Named separately from a partly priced run,
        # because the two have different fixes.
        return [Status.INCONCLUSIVE], [
            f"{gate.evidence_id} cost: no cost recorded (the run had no price table)"
        ]
    if rate is None:
        # Comparing USD against a EUR ceiling would mean inventing a rate.
        return [Status.INCONCLUSIVE], [
            f"{gate.evidence_id} cost: contract ceiling is EUR but the gate records cost_usd "
            "and the release declares no eur_per_usd rate, so the two are not comparable"
        ]

    statuses: list[Status] = []
    findings: list[str] = []
    if costs:
        worst_eur = max(costs) * rate
        if worst_eur > limit_eur:
            statuses.append(Status.FAIL)
            findings.append(
                f"{gate.evidence_id} cost: worst EUR {worst_eur:.4f} exceeds the {limit_eur} "
                f"ceiling (converted at a declared {rate} EUR/USD, an operator claim)"
            )
        elif not unusable and not unpriced:
            # Only when every scenario was priced. A ceiling met by the priced
            # subset is a statement about that subset, and this rule reports on
            # the run.
            statuses.append(Status.PASS)
            findings.append(
                f"{gate.evidence_id} cost: worst EUR {worst_eur:.4f} within the {limit_eur} "
                f"ceiling, converted at a declared {rate} EUR/USD (operator claim, not "
                "measured)"
            )
    if unpriced:
        statuses.append(Status.INCONCLUSIVE)
        findings.append(
            f"{gate.evidence_id} cost: {len(unpriced)} scenario(s) record no cost "
            f"({', '.join(sorted(unpriced)[:5])}), so the ceiling is unevidenced for them"
        )
    if unusable:
        statuses.append(Status.INCONCLUSIVE)
        findings.append(
            f"{gate.evidence_id} cost: {len(unusable)} scenario(s) record a cost that is not "
            f"a usable number ({', '.join(sorted(unusable)[:5])})"
        )
    return statuses, findings


def _rule_slo(
    rule: Rule, readings: list[Reading], release: ReleaseDescriptor | None = None, **_: object
) -> RuleOutcome:
    contracts = [r for r in readings if r.kind == "contract"]
    gates = [r for r in readings if r.kind == "gate-run"]
    if not contracts or not gates:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail="SLO needs both a contract and a gate run",
        )

    findings: list[str] = []
    statuses: list[Status] = []
    # Every collected artifact, not the first of each. `collect` takes a
    # repeated `-e gate-run=`, and reading one of several meant a second run
    # breaching a latency or cost ceiling was never looked at: the class it
    # belongs to would still reduce over it, but this rule, the only thing that
    # compares it against the agreed ceiling, never saw it.
    for contract in contracts:
        slo = contract.facts.get("slo") or {}
        for gate in gates:
            for produced, notes in (_slo_latency(slo, gate), _slo_cost(slo, gate, release)):
                statuses.extend(produced)
                findings.extend(notes)

    if not statuses:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.NOT_APPLICABLE,
            detail="the contract declares no SLO ceilings",
        )
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=worst(statuses),
        detail="; ".join(findings) if findings else "all declared ceilings hold",
        findings=findings,
    )


def _rule_state_verdict(rule: Rule, readings: list[Reading], **_: object) -> RuleOutcome:
    verdicts = [r for r in readings if r.kind == "state-verdict"]
    if not verdicts:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail="no state verdicts collected",
        )
    findings = [
        f"{r.facts.get('scenario_id')}: {r.status.value} ({r.detail})"
        for r in verdicts
        if r.status is not Status.PASS
    ]
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking,
        status=worst([r.status for r in verdicts]),
        detail=f"{len(verdicts)} state verdict(s), {len(findings)} not passing",
        findings=findings,
    )


def _rule_contract_change(
    rule: Rule, readings: list[Reading], release: ReleaseDescriptor, **_: object
) -> RuleOutcome:
    reports = [r for r in readings if r.kind == "blast-radius"]
    if not reports:
        if release.contract_change.declared:
            return RuleOutcome(
                id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
                detail="release declares a contract change but no blast radius report was collected",
            )
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail=(
                "no blast radius report; the release's claim that no contract changed is "
                "an unverified human declaration, not evidence"
            ),
        )

    ceiling = SEVERITY_RANK[rule.max_severity]
    findings: list[str] = []
    statuses: list[Status] = []
    for report in reports:
        statuses.append(report.status)
        for severity in ("breaking", "risky", "info"):
            count = report.facts.get(severity, 0)
            if count and SEVERITY_RANK[severity] > ceiling:
                findings.append(
                    f"{report.evidence_id}: {count} {severity} change group(s) exceed the "
                    f"{rule.max_severity!r} ceiling"
                )
    status = worst(statuses)
    if findings:
        status = worst([status, Status.FAIL])
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=status,
        detail=f"{len(reports)} report(s); {len(findings)} finding(s) above the ceiling",
        findings=findings,
    )


def _rule_regression(rule: Rule, **_: object) -> RuleOutcome:
    # Release-to-release comparison needs a previous signed pack. `compare` is
    # cut from v0.1 (it cannot feed a rule that runs before it), so this rule
    # reports honestly rather than silently passing.
    #
    # INCONCLUSIVE, not NOT_APPLICABLE. The lattice distinguishes them for a
    # reason: NOT_APPLICABLE means the question does not arise, INCONCLUSIVE
    # means it arises and has no answer. A policy that asks whether this release
    # regressed against the last one has asked a question that arises; the tool
    # simply cannot answer it. Reporting inapplicability instead would be the
    # tool declining a question on the policy's behalf.
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
        detail=(
            f"metric {rule.metric!r}: no baseline pack supplied, so no regression "
            "comparison was performed"
        ),
    )


def _rule_release_binding(rule: Rule, bindings: list, **_: object) -> RuleOutcome:
    if not bindings:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail="no binding checks ran",
        )
    findings = [
        f"{b.evidence_id} {b.check}: {b.status.value} ({b.detail})"
        for b in bindings
        if b.status is not Status.PASS
    ]
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking,
        status=worst([b.status for b in bindings]),
        detail=f"{len(bindings)} binding check(s), {len(findings)} not passing",
        findings=findings,
    )


def _rule_traceability_conflict(
    rule: Rule, readings: list[Reading], trace: TraceabilityMap, **_: object
) -> RuleOutcome:
    """Do independent producers agree on which requirements a task exercises?

    Every id involved exists in the contract, so referential integrity passes
    and catches nothing. The defect is semantic: two producers can each declare
    a different requirement set for the same task, and at most one of them can
    be right. Reporting the disagreement is the only honest response, because no
    automated check can tell which side is correct.
    """
    claims: dict[str, dict[str, set[str]]] = {}

    for entry in trace.entries:
        for task in entry.silobench_tasks:
            claims.setdefault(task, {}).setdefault("traceability-map", set()).add(entry.requirement)

    for reading in readings:
        if reading.kind == "state-verdict":
            task = reading.facts.get("silobench_task")
            reqs = set(reading.facts.get("requirements") or [])
            if task and reqs:
                claims.setdefault(task, {}).setdefault("statediff", set()).update(reqs)
        elif reading.kind == "blast-radius":
            # The canary's consumer map is the third opinion, carried inside its
            # report as affected[].requirement_ids, per scenario.
            for scenario, reqs in (reading.facts.get("affected_map") or {}).items():
                if reqs:
                    claims.setdefault(scenario, {}).setdefault("canary", set()).update(reqs)

    findings: list[str] = []
    for task, sources in sorted(claims.items()):
        stated = {src: reqs for src, reqs in sources.items() if reqs}
        if len(stated) < 2:
            continue
        distinct = {frozenset(reqs) for reqs in stated.values()}
        if len(distinct) > 1:
            rendered = "; ".join(
                f"{src} says {sorted(reqs)}" for src, reqs in sorted(stated.items())
            )
            findings.append(f"{task}: producers disagree: {rendered}")

    if findings:
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.FAIL,
            detail=f"{len(findings)} task(s) have contradictory requirement provenance",
            findings=findings,
        )
    comparable = [t for t, s in claims.items() if len({k for k, v in s.items() if v}) >= 2]
    if not comparable:
        # With fewer than two independent opinions there is nothing to compare,
        # and reporting agreement would claim a cross-check that never happened.
        return RuleOutcome(
            id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.INCONCLUSIVE,
            detail=(
                f"{len(claims)} task(s) carry provenance but none has two independent "
                "sources, so no cross-check was possible"
            ),
        )
    return RuleOutcome(
        id=rule.id, kind=rule.kind, blocking=rule.blocking, status=Status.PASS,
        detail=f"{len(comparable)} task(s) cross-checked, no contradictory provenance",
    )


_RULES = {
    "requirement_coverage": _rule_requirement_coverage,
    "assertion_density": _rule_assertion_density,
    "slo": _rule_slo,
    "state_verdict": _rule_state_verdict,
    "contract_change": _rule_contract_change,
    "regression": _rule_regression,
    "release_binding": _rule_release_binding,
    "traceability_conflict": _rule_traceability_conflict,
}


def evaluate_rules(
    policy: Policy,
    readings: list[Reading],
    trace: TraceabilityMap,
    release: ReleaseDescriptor,
    bindings: list,
) -> list[RuleOutcome]:
    outcomes = []
    for rule in policy.rules:
        handler = _RULES.get(rule.kind)
        if handler is None:
            raise PolicyError(f"rule {rule.id!r}: unimplemented kind {rule.kind!r}")
        outcomes.append(
            handler(
                rule=rule,
                readings=readings,
                trace=trace,
                release=release,
                bindings=bindings,
            )
        )
    return outcomes
