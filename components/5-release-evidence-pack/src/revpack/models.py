"""Schemas for every document this project reads or writes.

Every model forbids extra fields. A typo in a hand-written policy or release
descriptor is a loud failure rather than a silently ignored key, because a
silently ignored ``blocking: true`` is indistinguishable from a rule that does
not exist.

``Decision`` deliberately carries no timestamp. It must be a pure function of
(evidence bytes, policy, release descriptor, traceability map) so that ``verify``
can recompute it and compare. A wall-clock field would make every recomputation
differ and would quietly reduce the strongest check in the project to a no-op.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .lattice import REDUCERS, Status

SHA256 = r"^[a-f0-9]{64}$"

EvidenceKind = Literal[
    "contract",
    "transcript",
    "gate-run",
    "state-verdict",
    "blast-radius",
    "environment-golden",
    "silobench-verify",
    "prices",
]

# Inputs are copied into the pack and hashed like evidence, but they are the
# terms of the decision rather than observations of the release, so they are
# categorized separately in the manifest.
InputKind = Literal["policy", "traceability-map", "release-descriptor"]

DecisionState = Literal["GO", "NO_GO", "INCOMPLETE"]

RuleKind = Literal[
    "requirement_coverage",
    "assertion_density",
    "slo",
    "state_verdict",
    "contract_change",
    "regression",
    "release_binding",
    "traceability_conflict",
]

CoverProjection = Literal[
    "traceability_scenarios",
    "traceability_statediff_scenarios",
    "all_declared",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


class Producer(Strict):
    name: str
    version: str = "unknown"
    # How the version was obtained. Never inferred: an unknown version is
    # recorded as unknown rather than guessed from a sibling directory.
    version_source: Literal["declared", "detected", "unknown"] = "unknown"


class Vcs(Strict):
    commit: Optional[str] = None
    dirty: bool = False


class Envelope(Strict):
    format: Literal["evidence.v1"] = "evidence.v1"
    evidence_id: str
    kind: EvidenceKind
    path: str
    sha256: str = Field(pattern=SHA256)
    bytes: int = Field(ge=0, strict=True)
    collected_at: str
    detected_format: str
    producer: Producer
    vcs: Optional[Vcs] = None
    redacted: bool = False


class EnvelopeFile(Strict):
    format: Literal["envelopes.v1"] = "envelopes.v1"
    envelopes: list[Envelope] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# release descriptor
# ---------------------------------------------------------------------------


class Environment(Strict):
    # Strict scalar types, not bare int/bool. `Strict` only forbids extra fields;
    # it does not stop pydantic coercing `seed: "1"` to 1 or `docs_outage: "false"`
    # to True. These three are release identity, bound by equality against the
    # producers' environment, so a coerced string would bind cleanly against a
    # differently-typed value the producer actually recorded.
    seed: StrictInt
    schema_version: StrictInt
    docs_outage: StrictBool
    initial_state_hash: str = Field(pattern=SHA256)
    # SiloBench compares only default-profile runs against its committed golden,
    # so a task carrying ``is_default_run: false`` never has its final world
    # state checked against anything. That flag is written by the producer, which
    # means leaving the comparison's fate to it lets the producer decide which of
    # its own results get graded. This field moves the decision to the operator:
    # setting it declares that this release deliberately runs a non-default
    # profile, so an unverified final state is an expected, attributable gap
    # rather than one nobody noticed. It does not clear the run: the final state
    # is still ungraded, so the task stays inconclusive and the release
    # INCOMPLETE. It is a claim, not a measurement, and it is recorded in the pack
    # and covered by the signature like every other claim in the descriptor.
    expect_non_default_runs: bool = False


class ContractChange(Strict):
    """Whether MCP tool contracts changed in this release.

    ``declared: false`` is an unverified human claim, not evidence, and is
    reported as such. An earlier draft made blast-radius evidence conditional on
    a fingerprint being non-null, which let anyone suppress the requirement by
    declaring null. Requiring an explicit boolean at least puts a name against
    the claim.
    """

    declared: bool
    baseline_fingerprint: Optional[str] = Field(default=None, pattern=SHA256)
    candidate_fingerprint: Optional[str] = Field(default=None, pattern=SHA256)
    # For a no-change release: the contract fingerprint the canary report must show
    # on BOTH sides. Without it, equal report fingerprints corroborate nothing
    # about THIS release, because any unrelated contract pair, or a forged pair of
    # two identical digests (64 zeroes, say), is self-consistent. Declaring it
    # moors the report to the contract this release says did not change, so the
    # binding checks agreement against a value the release named rather than
    # against the report agreeing with itself.
    unchanged_fingerprint: Optional[str] = Field(default=None, pattern=SHA256)

    @model_validator(mode="after")
    def _fingerprints_consistent_with_declaration(self) -> "ContractChange":
        if self.declared and not (self.baseline_fingerprint and self.candidate_fingerprint):
            raise ValueError(
                "contract_change.declared is true, so both baseline_fingerprint and "
                "candidate_fingerprint are required to bind the canary report"
            )
        if self.declared and self.unchanged_fingerprint:
            raise ValueError(
                "contract_change.declared is true, so unchanged_fingerprint must be omitted; a "
                "declared change is bound by baseline_fingerprint and candidate_fingerprint"
            )
        if not self.declared and (self.baseline_fingerprint or self.candidate_fingerprint):
            # A no-change release binds against a declared unchanged_fingerprint,
            # not against a pair of change endpoints. Left settable, baseline and
            # candidate could be contradictory (baseline != candidate, which is
            # itself a change) while binding ignored them, so a descriptor that
            # declares no change and names two different contracts was accepted. A
            # no-change release names one unchanged hash, not two change hashes.
            raise ValueError(
                "contract_change.declared is false, so baseline_fingerprint and "
                "candidate_fingerprint must be omitted; a no-change release names its single "
                "unchanged_fingerprint, not two change hashes"
            )
        return self


class ReleaseDescriptor(Strict):
    release: Literal["release.v1"] = "release.v1"
    id: str = Field(min_length=1)
    contract_sha256: str = Field(pattern=SHA256)
    agent_model: str = Field(min_length=1)
    # Which gate run this release was evaluated from. Weak by nature: the gate's
    # run_id is a timezone-less local timestamp that can collide and that
    # survives promotion to a baseline. It is still the only run-level
    # identifier any producer emits, and binding against a declared value is
    # strictly better than binding against nothing.
    gate_run_id: Optional[str] = None
    environment: Environment
    contract_change: ContractChange
    transcript_sha256: Optional[str] = Field(default=None, pattern=SHA256)
    # Contracts state cost ceilings in EUR; agent-eval-gate records cost_usd and
    # no producer declares a rate. This is an operator declaration, reported as
    # such, not a measurement. Absent it, a EUR ceiling is unverifiable rather
    # than assumed satisfied.
    # allow_inf_nan=False: an infinite or NaN rate is not a measurement, and
    # `0 * inf` is NaN while `NaN > ceiling` is false, so an infinite rate would
    # let a blocking cost ceiling report PASS and render "nan within the ceiling".
    eur_per_usd: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)


# ---------------------------------------------------------------------------
# traceability
# ---------------------------------------------------------------------------


class TraceEntry(Strict):
    requirement: str = Field(min_length=1)
    silobench_tasks: list[str] = Field(default_factory=list)
    statediff_scenarios: list[str] = Field(default_factory=list)
    gate_scenarios: list[str] = Field(default_factory=list)
    # Unverifiable by machine, and required anyway: it forces the author to
    # state a reason and gives a reviewer something concrete to reject. The
    # defect this guards against is a join that is referentially valid and
    # semantically false.
    justification: str = Field(min_length=1)


class TraceabilityMap(Strict):
    map: Literal["traceability-map.v1"] = "traceability-map.v1"
    project: str = Field(min_length=1)
    contract_sha256: str = Field(pattern=SHA256)
    declared_by: str = Field(min_length=1)
    entries: list[TraceEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


class RequiredEvidence(Strict):
    kind: EvidenceKind
    min_count: int = Field(default=1, ge=0)
    # No default. Omitting it is a validation error, because a silent default
    # answers "several artifacts of this kind disagree" without anyone having
    # decided what the answer should be, and does it invisibly.
    reduce: str
    authoritative_evidence_id: Optional[str] = None
    must: list[str] = Field(default_factory=list)
    must_cover: Optional[CoverProjection] = None

    @model_validator(mode="after")
    def _check_reduce(self) -> "RequiredEvidence":
        if self.reduce not in REDUCERS:
            raise ValueError(f"reduce must be one of {list(REDUCERS)}, got {self.reduce!r}")
        if self.reduce == "authoritative" and not self.authoritative_evidence_id:
            raise ValueError(
                "reduce: authoritative requires authoritative_evidence_id naming "
                "which artifact decides"
            )
        return self


class Rule(Strict):
    id: str = Field(min_length=1)
    kind: RuleKind
    title: str = ""
    blocking: bool = True
    min_behavioral_checks: int = Field(default=1, ge=0)
    max_severity: Literal["breaking", "risky", "info"] = "info"
    metric: Optional[str] = None
    direction: Literal["at_most", "at_least"] = "at_most"
    tolerance_pct: float = 0.0

    @model_validator(mode="after")
    def _check_params(self) -> "Rule":
        if self.kind == "regression" and not self.metric:
            raise ValueError(f"rule {self.id}: kind 'regression' requires a metric")
        return self


class Policy(Strict):
    policy: Literal["release-policy.v1"] = "release-policy.v1"
    id: str = Field(min_length=1)
    required_evidence: list[RequiredEvidence] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------


class ClassOutcome(Strict):
    kind: str
    status: Status
    count: int
    min_count: int
    reduce: str
    members: list[str] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)
    detail: str = ""


class RuleOutcome(Strict):
    id: str
    kind: str
    blocking: bool
    status: Status
    detail: str = ""
    findings: list[str] = Field(default_factory=list)


class BindingOutcome(Strict):
    evidence_id: str
    kind: str
    check: str
    status: Status
    detail: str = ""


class TraceabilityOutcome(Strict):
    declared_entries: int = 0
    covered_requirements: list[str] = Field(default_factory=list)
    uncovered_requirements: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    dangling: list[str] = Field(default_factory=list)


# Bump when the MEANING of a rule, reducer, or precedence changes, so that a
# later version can tell "this pack was decided under rules I no longer
# implement" apart from "this pack was tampered with". Cosmetic changes to
# messages must never bump it, and must never affect a verdict.
#
# v2: several checks that used to report a pass now report what they actually
# established. Requirement coverage resolves traceability ids against collected
# evidence instead of accepting any non-empty id; a SiloBench run flagged as
# non-default is inconclusive rather than passing, and a release descriptor that
# declares it expects one makes the gap attributable but still does not clear it;
# a StateDiff check whose status is outside the known vocabulary no longer counts
# as passing; a SiloBench verdict that claims `passed` while listing failures is
# reported as self-contradictory. A v1 pack
# can therefore recompute to a different verdict under this build, which is
# exactly the case this number exists to keep apart from tampering.
#
# v3: several checks that used to stand in for a measurement now report that
# they have none. A trajectory that records no per-step latency no longer totals
# to zero and no longer satisfies a latency ceiling; a `clean` canary report must
# state how many replays failed before that state is read as a pass; every
# SiloBench task must record the profile it ran under; a gate scenario naming no
# model is no longer dropped from the model comparison, so silence cannot read as
# agreement; a committed golden must identify itself and carry SHA-256 task
# hashes; equal canary fingerprints corroborate a no-change declaration only when
# they are digests. A requirement is summarized as covered only when the same
# resolution the blocking coverage rule performs finds evidence for it, so the
# two halves of a decision can no longer contradict each other. A v2 pack can
# therefore recompute to a different verdict under this build.
#
# v4: three more readings that stood in for an answer now report that they have
# none. A gate run whose `mode` is outside agent-eval-gate's own `live | replay`
# vocabulary is refused instead of being read as a live run, which used to
# disarm both replay protections at once; a StateDiff verdict whose checks are
# empty or entirely `not_applicable` is inconclusive rather than passing,
# because "nothing failed" is not the same as "a predicate held"; a cost ceiling
# is unevidenced for a scenario the run put no price against, instead of being
# satisfied by the priced scenarios beside it. A v3 pack can therefore recompute
# to a different verdict under this build.
SEMANTICS_VERSION = 5


class Decision(Strict):
    format: Literal["decision.v1"] = "decision.v1"
    # Who computed this, and under which rule semantics. Recording the producing
    # tool version is the one thing every upstream producer in this toolchain
    # fails to do, and a release dossier that repeats the omission has no
    # standing to complain about it.
    revpack_version: str = "unknown"
    semantics_version: int = SEMANTICS_VERSION
    state: DecisionState
    policy_id: str
    release_id: str
    inputs_digest: str = Field(pattern=SHA256)
    evidence_classes: list[ClassOutcome] = Field(default_factory=list)
    rules: list[RuleOutcome] = Field(default_factory=list)
    bindings: list[BindingOutcome] = Field(default_factory=list)
    traceability: TraceabilityOutcome = Field(default_factory=TraceabilityOutcome)
    fail_count: int = 0
    inconclusive_count: int = 0
    derivations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def semantic_core(self) -> dict:
        """The decision-bearing fields, with all human-readable prose removed.

        Verification compares this rather than the whole document, so what is
        left out of it is what a forger may edit inside a signed pack without
        being contradicted. The test is not "did this change the verdict on this
        pack" but "could a reader act differently on it": an evidence class that
        names EV-0002 as its members rather than EV-0001 describes a different
        artifact as the deciding one, and a class that reduced by
        ``authoritative`` rather than ``worst`` reached its status by a different
        rule. Neither difference has to move a status to make the archived
        decision say something it did not say.

        Excluded, deliberately and exhaustively: ``detail`` on every outcome,
        ``findings`` on a rule, the rendered ``conflicts`` strings (their count
        is here), ``derivations``, and ``notes``. Those are sentences written for
        a person, they are regenerated from this build's wording on every
        recomputation, and including them would turn an improved message into an
        accusation of tampering against an untouched pack. Also excluded:
        ``format``, which is a constant the schema pins, and
        ``revpack_version``/``semantics_version``, which describe the producing
        build rather than the release, and the second of which is checked against
        the signed manifest before this comparison is reached.
        """
        return {
            "state": self.state,
            "policy_id": self.policy_id,
            "release_id": self.release_id,
            "inputs_digest": self.inputs_digest,
            "fail_count": self.fail_count,
            "inconclusive_count": self.inconclusive_count,
            "evidence_classes": sorted(
                [
                    {
                        "kind": c.kind,
                        "status": c.status.value,
                        "count": c.count,
                        # The terms the class was judged under, and the artifacts
                        # it was judged over. Without them a pre-seal edit could
                        # lower a min_count the class no longer meets, swap the
                        # reducer that produced the status, or rewrite which
                        # evidence the class was built from, and verification
                        # would file every one of those under explanatory wording.
                        "min_count": c.min_count,
                        "reduce": c.reduce,
                        "members": sorted(c.members),
                        "uncovered": sorted(c.uncovered),
                    }
                    for c in self.evidence_classes
                ],
                key=lambda item: item["kind"],
            ),
            "rules": sorted(
                [
                    {"id": r.id, "kind": r.kind, "blocking": r.blocking, "status": r.status.value}
                    for r in self.rules
                ],
                key=lambda item: item["id"],
            ),
            "bindings": sorted(
                [
                    {
                        "evidence_id": b.evidence_id,
                        # Which kind of artifact this check bound. A binding
                        # outcome relabelled from one kind to another names a
                        # different artifact as the one that was tied to the
                        # release, and nothing else in the core would notice.
                        "kind": b.kind,
                        "check": b.check,
                        "status": b.status.value,
                    }
                    for b in self.bindings
                ],
                key=lambda item: (item["evidence_id"], item["check"]),
            ),
            "traceability": {
                # How many entries the map declared. A summary that reports the
                # same covered set over a different number of declared entries is
                # describing a different map.
                "declared_entries": self.traceability.declared_entries,
                "covered": sorted(self.traceability.covered_requirements),
                "uncovered": sorted(self.traceability.uncovered_requirements),
                "dangling": sorted(self.traceability.dangling),
                "conflict_count": len(self.traceability.conflicts),
            },
        }


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


class ManifestEntry(Strict):
    path: str
    sha256: str = Field(pattern=SHA256)
    bytes: int = Field(ge=0, strict=True)
    category: Literal["evidence", "input", "derived", "index"]


class Manifest(Strict):
    format: Literal["manifest.v1"] = "manifest.v1"
    pack_id: str
    # Repeated here as well as in the decision because the manifest is the first
    # file a verifier opens. An archival artifact that cannot say which tool
    # produced it forces every future reader to guess.
    revpack_version: str = "unknown"
    semantics_version: int = SEMANTICS_VERSION
    entries: list[ManifestEntry] = Field(default_factory=list)
    attestation: Optional[dict] = None
