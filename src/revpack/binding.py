"""Release binding: does this artifact describe the release under decision?

Without this, four artifacts from four different releases can each be green and
aggregate to a clean GO. Presence and greenness are not the same as relevance,
and an evidence pack that cannot tell them apart is a filing cabinet, not a
gate.

Two limits, stated here rather than buried.

First, matching ``before_state_hash`` proves a common seeded starting
environment. It does NOT prove the state oracle graded the same agent execution
the gate recorded. Six of SiloBench's twelve golden task hashes are identical
(the read-only tasks all equal the initial hash), and no producer emits a run
identifier shared across tools. Closing that gap needs an upstream change: a run
id propagated from the gate into the state capture. Until then this module
reports environment-level binding and says so.

Second, the release descriptor is trusted input. Binding checks that the
evidence is consistent WITH the declaration, not that the declaration is true.
Someone who controls ``release.yaml`` can declare an environment hash that
matches whatever evidence they hold, and it will bind. What the design buys is
attribution rather than prevention: the descriptor is copied into the pack,
hashed, and covered by the signature, so a false declaration is permanently
attached to the key that signed it. A tool cannot tell you the truth about a
release; it can make lying about one leave a trace.
"""

from __future__ import annotations

from .lattice import Status
from .models import BindingOutcome, ReleaseDescriptor
from .parsers import ID_BINDINGS, SHA256_HEX, Reading

RUN_LEVEL_GAP = (
    "environment-level binding only: a matching initial state hash proves a common "
    "seeded environment, not that this verdict graded this gate run"
)


def _is_sha256(value: object) -> bool:
    """Is this a value an equality test between fingerprints can mean anything on?

    Comparing two fields for equality only establishes something when both are
    the kind of value the producer computes. Any two identical strings are
    equal, so without this a report fingerprinting neither side corroborates
    whatever the release happens to declare.
    """
    return isinstance(value, str) and SHA256_HEX.fullmatch(value) is not None


def _outcome(reading: Reading, check: str, ok: bool, detail: str) -> BindingOutcome:
    return BindingOutcome(
        evidence_id=reading.evidence_id,
        kind=reading.kind,
        check=check,
        status=Status.PASS if ok else Status.FAIL,
        detail=detail,
    )


def _bind_contract(release: ReleaseDescriptor, reading: Reading) -> list[BindingOutcome]:
    actual = reading.facts.get("sha256")
    return [
        _outcome(
            reading,
            "contract_sha256",
            actual == release.contract_sha256,
            f"contract hash {actual} vs declared {release.contract_sha256}",
        )
    ]


def _bind_state_verdict(release: ReleaseDescriptor, reading: Reading) -> list[BindingOutcome]:
    actual = reading.facts.get("before_state_hash")
    if actual is None:
        # An error verdict carries no artifact fingerprints at all, so there is
        # nothing to bind. Unbindable is inconclusive, never ignored.
        return [
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="before_state_hash",
                status=Status.INCONCLUSIVE,
                detail="verdict carries no artifact fingerprints, so it cannot be bound",
            )
        ]
    ok = actual == release.environment.initial_state_hash
    outcome = _outcome(
        reading,
        "before_state_hash",
        ok,
        f"before state {actual} vs declared initial "
        f"{release.environment.initial_state_hash}",
    )
    if ok:
        outcome.detail += f"; {RUN_LEVEL_GAP}"
    outcomes = [outcome]

    # The schema version the verdict was graded under. A matching before-state
    # hash proves a common seeded start, but six of the twelve golden hashes are
    # identical, so a schema-2 verdict can share a schema-1 release's initial
    # hash and bind on that alone. The verdict names its own schema in
    # artifacts.schema_version, so it is compared rather than trusted.
    declared_schema = release.environment.schema_version
    actual_schema = reading.facts.get("schema_version")
    if actual_schema is None:
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="schema_version",
                status=Status.INCONCLUSIVE,
                detail=(
                    "verdict records no artifacts.schema_version, so the environment it was "
                    "graded under cannot be confirmed"
                ),
            )
        )
    else:
        outcomes.append(
            _outcome(
                reading,
                "schema_version",
                actual_schema == declared_schema,
                f"verdict schema {actual_schema!r} vs declared {declared_schema!r}",
            )
        )
    return outcomes


def _bind_environment_golden(
    release: ReleaseDescriptor, reading: Reading
) -> list[BindingOutcome]:
    key = f"v{release.environment.schema_version}"
    actual = (reading.facts.get("initial") or {}).get(key)
    return [
        _outcome(
            reading,
            "initial_state_hash",
            actual == release.environment.initial_state_hash,
            f"golden initial[{key}] {actual} vs declared "
            f"{release.environment.initial_state_hash}",
        )
    ]


def _bind_silobench(release: ReleaseDescriptor, reading: Reading) -> list[BindingOutcome]:
    """A verify run under a different profile describes a different world.

    SiloBench's schema version and docs-outage flag are the entire configuration
    surface. A passing schema-2 run is not evidence about a schema-1 release,
    and without this check it would bind cleanly because nothing else in the
    artifact identifies the environment.
    """
    declared_schema = release.environment.schema_version
    declared_outage = release.environment.docs_outage
    schemas = [s for s in (reading.facts.get("schema_versions") or []) if s is not None]
    outages = reading.facts.get("docs_outages") or []

    outcomes = []
    if not schemas:
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="environment_profile",
                status=Status.INCONCLUSIVE,
                detail="verify output records no profile, so the environment cannot be confirmed",
            )
        )
        return outcomes

    wrong_schema = [s for s in schemas if s != declared_schema]
    wrong_outage = [o for o in outages if o != declared_outage]
    ok = not wrong_schema and not wrong_outage
    outcomes.append(
        _outcome(
            reading,
            "environment_profile",
            ok,
            (
                f"all tasks ran under schema {declared_schema}, docs_outage {declared_outage}"
                if ok
                else f"tasks ran under schema(s) {sorted(set(schemas))} and docs_outage "
                f"{sorted(set(outages))}, but the release declares schema {declared_schema} "
                f"and docs_outage {declared_outage}"
            ),
        )
    )
    return outcomes


def _bind_transcript(release: ReleaseDescriptor, reading: Reading) -> BindingOutcome:
    """Transcript bytes must be the ones the contract was compiled from."""
    declared = release.transcript_sha256
    actual = reading.facts.get("sha256")
    if not declared:
        return BindingOutcome(
            evidence_id=reading.evidence_id,
            kind=reading.kind,
            check="transcript_sha256",
            status=Status.INCONCLUSIVE,
            detail=(
                "the release declares no transcript_sha256, so these bytes cannot be tied "
                "to the contract's pinned transcript"
            ),
        )
    return BindingOutcome(
        evidence_id=reading.evidence_id,
        kind=reading.kind,
        check="transcript_sha256",
        status=Status.PASS if actual == declared else Status.FAIL,
        detail=f"transcript hash {actual} vs declared {declared}",
    )


def _bind_gate_run(release: ReleaseDescriptor, reading: Reading) -> list[BindingOutcome]:
    outcomes: list[BindingOutcome] = []
    scenarios = reading.facts.get("scenarios") or {}

    # The substitution check. ReplayAdapter never validates that the trajectory
    # inside a fixture belongs to the scenario the fixture is filed under, so a
    # run can carry results for scenarios it did not execute.
    # Read from a structured flag, never inferred by string-matching a
    # human-readable detail message: a binding decision that depends on the
    # wording of a log line breaks the moment someone improves the wording.
    substituted = sorted(s for s, i in scenarios.items() if i.get("id_binding") == "mismatch")
    unverifiable = sorted(s for s, i in scenarios.items() if i.get("id_binding") == "unverifiable")
    # Membership of the closed set the parser records, not inequality against two
    # strings. The passing branch below claims every trajectory carries its own
    # scenario id, and it is reached by the absence of "mismatch" and
    # "unverifiable", so any third value would let that claim be made about a
    # scenario whose identity nothing compared.
    unreadable = sorted(
        f"{s} ({i.get('id_binding')!r})"
        for s, i in scenarios.items()
        if i.get("id_binding") not in ID_BINDINGS
    )

    if substituted:
        status, detail = Status.FAIL, f"fixture substitution in: {', '.join(substituted)}"
    elif unverifiable:
        # Failing to confirm identity is not the same as confirming a mismatch.
        # Collapsing them would report a clean bind on an artifact whose
        # identity was never established.
        status, detail = (
            Status.INCONCLUSIVE,
            "trajectory scenario id missing, so identity is unverifiable for: "
            + ", ".join(unverifiable),
        )
    elif unreadable:
        status, detail = (
            Status.INCONCLUSIVE,
            "scenario identity is recorded in a form this build does not interpret for: "
            + ", ".join(unreadable),
        )
    elif not scenarios:
        status, detail = Status.INCONCLUSIVE, "the run contains no scenario results"
    else:
        status, detail = Status.PASS, "every result's trajectory carries its own scenario id"

    outcomes.append(
        BindingOutcome(
            evidence_id=reading.evidence_id,
            kind=reading.kind,
            check="scenario_id_matches_trajectory",
            status=status,
            detail=detail,
        )
    )

    # Run identity. Without this the gate run binds on self-consistency alone,
    # and a run from another release with the same scenario ids and model would
    # bind cleanly.
    declared_run = release.gate_run_id
    actual_run = reading.facts.get("run_id") or ""
    if not declared_run:
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="gate_run_id",
                status=Status.INCONCLUSIVE,
                detail=(
                    "the release declares no gate_run_id, so this run cannot be tied to "
                    "this release by anything stronger than its scenario ids"
                ),
            )
        )
    else:
        outcomes.append(
            _outcome(
                reading,
                "gate_run_id",
                actual_run == declared_run,
                f"run_id {actual_run!r} vs declared {declared_run!r}"
                + ("; note run_id is a local timestamp and can collide" if actual_run == declared_run else ""),
            )
        )

    models = reading.facts.get("models") or []
    mode = reading.facts.get("mode")
    if not models:
        # An empty model set makes a mismatch check vacuously true: nothing was
        # compared, yet the old code reported "all scenarios ran on <declared>".
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="agent_model",
                status=Status.INCONCLUSIVE,
                detail="no scenario records a model, so the declared model cannot be confirmed",
            )
        )
    elif mode == "replay":
        # In replay the model recorded per scenario is whatever produced the
        # fixture. It is a historical fact about the recording, not evidence
        # that this release runs on the declared model.
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="agent_model",
                status=Status.INCONCLUSIVE,
                detail=(
                    f"replay run: the recorded model(s) {models or ['<none>']} are a fact "
                    f"about when the fixtures were captured, so they cannot evidence that "
                    f"this release runs on {release.agent_model!r}, even when the strings "
                    f"happen to match"
                ),
            )
        )
    else:
        mismatched = sorted({m for m in models if m != release.agent_model})
        # Scenarios that recorded no model at all. They were filtered out of
        # `models` upstream, so a run where one scenario named the declared model
        # and another named none reported "all scenarios ran on <declared>": a
        # claim about every scenario, established from the ones that answered. A
        # scenario that did not say which model it used is not a scenario that
        # used the right one, and it is not a mismatch either, so it is neither
        # passed nor failed.
        unrecorded = reading.facts.get("scenarios_without_model") or []
        # The run's own top-level agent_model. In a live run it names the model the
        # gate was configured with, so it should agree with both the release and
        # the per-scenario trajectories. It was stored and never read, so a run
        # whose header said one model while its trajectories (matching the release)
        # said another bound cleanly: a run contradicting itself about what it ran.
        top_model = reading.facts.get("agent_model_field")
        header_conflict = (
            isinstance(top_model, str) and top_model
            and top_model != "replay" and top_model != release.agent_model
        )
        if mismatched:
            status, detail = Status.FAIL, f"scenarios ran on unexpected model(s): {mismatched}"
        elif header_conflict:
            status, detail = (
                Status.FAIL,
                f"the run's top-level agent_model is {top_model!r} but the release declares "
                f"{release.agent_model!r}; the run contradicts itself about what it ran",
            )
        elif unrecorded:
            status, detail = (
                Status.INCONCLUSIVE,
                f"{len(unrecorded)} scenario(s) record no model "
                f"({', '.join(unrecorded[:5])}), so they cannot evidence that this release "
                f"ran on {release.agent_model!r}",
            )
        else:
            status, detail = Status.PASS, f"all scenarios ran on {release.agent_model!r}"
        outcomes.append(
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="agent_model",
                status=status,
                detail=detail,
            )
        )
    return outcomes


def _bind_blast_radius(release: ReleaseDescriptor, reading: Reading) -> list[BindingOutcome]:
    change = release.contract_change
    baseline = reading.facts.get("baseline_fingerprint")
    candidate = reading.facts.get("candidate_fingerprint")

    if not change.declared:
        # The release claims no MCP contract changed. That claim is checkable
        # rather than merely trusted: the canary report fingerprints both sides,
        # so identical fingerprints corroborate the declaration and differing
        # ones contradict it.
        if baseline is None or candidate is None:
            return [
                BindingOutcome(
                    evidence_id=reading.evidence_id,
                    kind=reading.kind,
                    check="contract_fingerprints",
                    status=Status.INCONCLUSIVE,
                    detail="report carries no contract fingerprints, so the release's "
                    "no-change declaration cannot be corroborated",
                )
            ]
        unusable = sorted(
            f"{label} {value!r}"
            for label, value in (("baseline", baseline), ("candidate", candidate))
            if not _is_sha256(value)
        )
        if unusable:
            # Equality was the whole check, and equality between two values that
            # are not contract fingerprints establishes nothing: "x" equals "x",
            # so a report that fingerprinted neither side confirmed the release's
            # no-change declaration and contributed a PASS to the decision. A
            # corroboration is only as strong as the shape of what it compares.
            return [
                BindingOutcome(
                    evidence_id=reading.evidence_id,
                    kind=reading.kind,
                    check="contract_fingerprints",
                    status=Status.INCONCLUSIVE,
                    detail=(
                        f"report carries contract fingerprint(s) that are not SHA-256 "
                        f"digests ({', '.join(unusable)}), so their agreement corroborates "
                        "nothing about the release's no-change declaration"
                    ),
                )
            ]
        declared_unchanged = change.unchanged_fingerprint
        if declared_unchanged is None:
            # Equality between two report fingerprints establishes that the report
            # is self-consistent, not that it is about this release. Any unrelated
            # contract pair agrees with itself, and two identical forged digests
            # (64 zeroes) agree perfectly, so agreement alone corroborated a
            # no-change declaration for a contract nobody tied to the release.
            # Without a declared fingerprint to compare against, the report is
            # unmoored and the declaration is uncorroborated, not confirmed.
            return [
                BindingOutcome(
                    evidence_id=reading.evidence_id,
                    kind=reading.kind,
                    check="contract_fingerprints",
                    status=Status.INCONCLUSIVE,
                    detail=(
                        "release declares no contract change but names no "
                        "contract_change.unchanged_fingerprint, so the report's fingerprints are "
                        "not tied to this release and their agreement corroborates nothing; "
                        "declare the unchanged fingerprint to bind the report"
                    ),
                )
            ]
        agrees = baseline == candidate == declared_unchanged
        if agrees:
            detail = (
                f"release declares no contract change and the report fingerprints both sides "
                f"as the declared unchanged contract ({declared_unchanged[:12]})"
            )
        elif baseline != candidate:
            detail = (
                f"release declares no contract change but the report compares baseline "
                f"{baseline[:12]} against candidate {candidate[:12]}"
            )
        else:
            detail = (
                f"release declares its unchanged contract is {declared_unchanged[:12]} but the "
                f"report fingerprints both sides as {baseline[:12]}; the report is about a "
                "different contract than the one this release names"
            )
        return [
            BindingOutcome(
                evidence_id=reading.evidence_id,
                kind=reading.kind,
                check="contract_fingerprints",
                status=Status.PASS if agrees else Status.FAIL,
                detail=detail,
            )
        ]

    outcomes = []
    for label, declared in (
        ("baseline_fingerprint", change.baseline_fingerprint),
        ("candidate_fingerprint", change.candidate_fingerprint),
    ):
        actual = reading.facts.get(label)
        outcomes.append(
            _outcome(
                reading,
                label,
                actual == declared,
                f"report {label} {actual} vs declared {declared}",
            )
        )
    return outcomes


# Kinds that carry nothing a release can be bound against, with the reason.
# Listed rather than left to fall off the end of a chain of `elif`s: `prices`
# had no branch at all, so a stale price table was sealed into a pack as
# evidence bound to nothing, and neither the decision nor the report said so.
UNBINDABLE_KINDS = {
    "prices": (
        "a price table is operator-supplied reference data and carries no field that ties "
        "it to this release, so nothing about it can be bound"
    ),
}

_BINDERS = {
    "contract": _bind_contract,
    "gate-run": _bind_gate_run,
    "state-verdict": _bind_state_verdict,
    "environment-golden": _bind_environment_golden,
    "blast-radius": _bind_blast_radius,
    "silobench-verify": _bind_silobench,
    "transcript": lambda release, reading: [_bind_transcript(release, reading)],
}


def bind(release: ReleaseDescriptor, readings: list[Reading]) -> list[BindingOutcome]:
    """One or more binding outcomes for every reading, with no silent gaps.

    A table rather than a chain of branches, so that an evidence kind with no
    binder produces an INCONCLUSIVE outcome instead of nothing at all. Producing
    nothing is the dangerous shape: `_rule_release_binding` reduces over the
    outcomes it is given, so an artifact that contributes no outcome cannot
    lower the result, and an unbound artifact ends up sealed into the pack under
    a passing release_binding rule.
    """
    outcomes: list[BindingOutcome] = []
    for reading in readings:
        binder = _BINDERS.get(reading.kind)
        if binder is None:
            outcomes.append(
                BindingOutcome(
                    evidence_id=reading.evidence_id,
                    kind=reading.kind,
                    check="release_binding",
                    status=Status.INCONCLUSIVE,
                    detail=UNBINDABLE_KINDS.get(
                        reading.kind,
                        f"evidence kind {reading.kind!r} has no binding check, so this "
                        "artifact cannot be tied to the release under decision",
                    ),
                )
            )
            continue
        outcomes.extend(binder(release, reading))
    return outcomes
