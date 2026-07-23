"""Producer parsers: five artifact families, five different ideas of a verdict.

Every trap handled here was verified against the producing source, not against
its documentation. Where a producer's own aggregate would hide a failure, the
parser reads past it. Where a producer serializes no verdict at all, the parser
recomputes one and records that it did.

The parsers are single-file and pure. Checks that need more than one artifact
(the SiloBench golden comparison needs the golden file, which is separate
evidence) live in ``cross_reference`` below, which runs after every file is
parsed. Keeping that separation explicit stops a parser from quietly depending
on collection order.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import InputError
from .lattice import Status, worst
from .producers import StateDiffVerdict, validate_producer

# A SHA-256 digest as this project writes and compares them: 64 lowercase hex
# characters. Matched with ``fullmatch`` rather than a ``$``-anchored pattern,
# because ``$`` also matches before a trailing newline and would accept a value
# no producer ever emitted.
SHA256_HEX = re.compile(r"[a-f0-9]{64}")

# ---------------------------------------------------------------------------
# behavioral check classification (agent-eval-gate)
# ---------------------------------------------------------------------------

# ``Checks.must_not_error`` defaults to True, so ``run_checks`` injects a
# ``no_tool_errors`` result into nearly every scenario. Counting all checks
# would therefore let a scenario that asserts nothing about agent behaviour
# satisfy an assertion-density rule. Budget checks are resource guards and
# state_checks sentinels are fail-closed markers; neither asserts correctness.
NON_BEHAVIORAL_EXACT = frozenset({"no_harness_error", "no_tool_errors"})
NON_BEHAVIORAL_PREFIXES = ("budget:", "state_checks:")

BEHAVIORAL_EXACT = frozenset({"expected_tools_ordered"})
BEHAVIORAL_PREFIXES = (
    "expected_tool:",
    "forbidden_tool:",
    "max_calls:",
    "output_contains:",
    "output_not_contains:",
)

# Externally merged oracle namespaces, as a CLOSED set. agent-eval-gate's
# --state-checks merges whatever names a file supplies, so treating any
# colon-bearing name as a behavioral assertion would let a single line of
# `{"name": "junk:anything", "passed": true}` satisfy the mandatory
# assertion-density rule without asserting anything about the agent. Adding a
# namespace here is a deliberate decision to trust that oracle.
ORACLE_PREFIXES = ("statediff:",)


def classify_check(name: str) -> tuple[bool, str]:
    """Return ``(is_behavioral, reason)`` for a gate check name.

    Unrecognized names are NOT counted, whatever their shape. That direction is
    deliberate: an uncounted name yields INCONCLUSIVE, which cannot clear a
    release, whereas counting one lets an unknown check stand in for evidence of
    correct behaviour.
    """
    if name in NON_BEHAVIORAL_EXACT:
        return False, "harness health check"
    if name.startswith(NON_BEHAVIORAL_PREFIXES):
        return False, "resource guard or fail-closed sentinel"
    if name in BEHAVIORAL_EXACT:
        return True, "configured behavioral assertion"
    # A prefix-based name must carry an identifier after the prefix. The suffix is
    # what names the tool, the check, or the oracle result, so `expected_tool:`
    # with nothing after the colon asserts nothing about the agent and must not be
    # counted; counting it lets a scenario clear the mandatory assertion-density
    # rule while identifying no behaviour. Two such names would also be identical
    # rather than distinct assertions.
    for prefix in BEHAVIORAL_PREFIXES:
        if name.startswith(prefix):
            if len(name) > len(prefix):
                return True, "configured behavioral assertion"
            return False, "behavioral prefix with no identifier, names nothing"
    for prefix in ORACLE_PREFIXES:
        if name.startswith(prefix):
            if len(name) > len(prefix):
                return True, "external oracle result from a recognized namespace"
            return False, "oracle prefix with no identifier, names nothing"
    return False, "unrecognized check name, not counted"


def _mapping(value: Any, path: str, what: str) -> dict:
    """Return ``value`` as a mapping, or raise a clean InputError.

    JSON permits a string or list anywhere an object is expected, and the
    resulting ``AttributeError`` from ``.get()`` would escape the CLI's error
    handling and surface as a traceback rather than the exit code 2 the tool
    promises for unusable input.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InputError(f"{path}: {what} must be a JSON object, got {type(value).__name__}")
    return value


def _sequence(value: Any, path: str, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(f"{path}: {what} must be a JSON array, got {type(value).__name__}")
    return value


def _string_list(value: Any, path: str, what: str) -> list[str]:
    """Read a JSON array of identifiers, refusing a member that is not one.

    Two failures hide behind ``list(value or [])``. A string is iterable, so a
    field serialized as ``"REQ-007"`` became the seven characters of its own
    name and was then compared, as a set, against another producer's requirement
    ids: a silent rewrite that no downstream check can see. A mixed array aborts
    ``sorted`` with a TypeError instead, which surfaces as a traceback rather
    than the exit 2 this tool promises for unusable input.

    Narrowing to the members that happen to be strings is not an option either.
    These lists are the claims one producer makes about which requirement a task
    exercises, and dropping the unreadable ones would shrink a claim set that the
    conflict rule compares for disagreement, which is how two producers stop
    disagreeing without either of them changing its mind.
    """
    items = _sequence(value, path, what)
    unusable = [
        f"{position} ({item!r})"
        for position, item in enumerate(items)
        if not isinstance(item, str)
    ]
    if unusable:
        raise InputError(
            f"{path}: {what} carries member(s) that are not strings at index "
            f"{', '.join(unusable)}, so they name nothing this pack can trace or compare"
        )
    return items


def _optional_flag(container: dict, key: str, path: str, what: str) -> Optional[bool]:
    """Read a JSON boolean that the producer is allowed to omit.

    Absent and False are different answers and must stay distinguishable.
    ``bool(container.get(key))`` collapses them, which turns "the producer
    stopped emitting this field" into "the producer said no" and silently
    retires whatever check the flag gates. The caller decides what an absent
    answer means; it is not this function's job to invent one.
    """
    if key not in container:
        return None
    value = container[key]
    if not isinstance(value, bool):
        raise InputError(
            f"{path}: {what} has a non-boolean {key!r} ({value!r}), which cannot be "
            f"coerced without picking an answer the artifact did not give"
        )
    return value


def _optional_string(container: dict, key: str, path: str, what: str) -> Optional[str]:
    """Read a JSON string the producer may omit or leave null, refusing a value
    that only looks absent.

    Truthiness-testing an error field reads ``false``, ``0``, ``{}``, ``[]``, and
    ``""`` as the absence of an error, so a malformed error slot silently clears
    the branch that treats a reported failure as one. Absent and null are the
    only two answers that mean no error; every other type is a producer that
    recorded something this build cannot render, so it is refused rather than
    read as silence. The caller then tests ``is not None`` rather than
    truthiness, so an empty string, which is still a value the producer wrote, is
    not mistaken for silence either.
    """
    if key not in container or container[key] is None:
        return None
    value = container[key]
    if not isinstance(value, str):
        raise InputError(
            f"{path}: {what} field {key!r} must be a JSON string or null, got "
            f"{type(value).__name__} {value!r}"
        )
    return value


def _flag(container: dict, key: str, path: str, what: str) -> bool:
    """Read a JSON boolean, refusing anything that merely looks like one.

    ``not value`` and ``bool(value)`` are truthiness tests, and every non-empty
    string is truthy, so a producer emitting ``"passed": "false"`` had its
    failing check counted as passing. Coercion cannot be made safe here: the
    string "false" and the string "true" are equally truthy, so any coercion
    silently picks the answer that clears the release.

    A missing key is refused for the same reason. It is not a default of False
    waiting to be applied; it is a producer that did not say, and the parser's
    job is to report what the artifact contains rather than to fill it in.
    """
    if key not in container:
        raise InputError(f"{path}: {what} has no {key!r} field, so it reports no result")
    value = container[key]
    if not isinstance(value, bool):
        raise InputError(
            f"{path}: {what} field {key!r} must be a JSON boolean, got "
            f"{type(value).__name__} {value!r}"
        )
    return value


def _count(container: dict, key: str, path: str, what: str) -> int:
    """Read a JSON integer counter the producer is not allowed to leave out.

    ``container.get(key) or 0`` answers on the producer's behalf when the
    producer said nothing, and the answer it invents is always the one that
    clears the check that reads it. A count is not a truthy value either: a
    string, a float, or a negative number is a field this build cannot compare
    against zero, so it is refused rather than coerced into whichever reading
    happens to pass.
    """
    if key not in container:
        raise InputError(
            f"{path}: {what} records no {key!r}, so it states no count for a check that "
            "reads one"
        )
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError(
            f"{path}: {what} field {key!r} must be a non-negative JSON integer, got "
            f"{type(value).__name__} {value!r}"
        )
    return value


@dataclass
class Reading:
    """One parsed artifact, normalized."""

    evidence_id: str
    kind: str
    path: str
    status: Status
    detail: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    derivations: list[str] = field(default_factory=list)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads``'s object hook: a repeated object key is a hard error.

    The default behaviour keeps the last value and discards the earlier one
    silently, and nothing downstream can see that it happened. Every verdict,
    digest, seal, and recomputation in this project runs over the PARSED
    structure, so ``{"passed": false, "passed": true}`` is indistinguishable from
    ``{"passed": true}`` from the first check onwards: the interpretation that
    clears the release survives the whole pipeline and the pack records no
    disagreement, because none was ever visible. A reviewer opening the file
    reads the first value; the tool graded the last.

    The sibling StateDiff adapter refuses these for the same reason, and the
    wording is kept in that spirit so an operator meets one message across the
    toolchain rather than two.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise InputError(f"duplicate JSON object key {key!r}")
        seen[key] = value
    return seen


def load_json(path: str | Path) -> Any:
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except InputError as exc:
        # Raised by the hook above from inside json.loads, so it names the key
        # and nothing else. Re-blamed on the file here, because a message that
        # does not say which artifact carries the repeat is not actionable.
        raise InputError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # Not a subclass of OSError or JSONDecodeError. Without this the tool
        # dies on a traceback instead of the clean exit 2 its contract promises.
        raise InputError(f"{path} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON: {exc}") from exc


def detect_format(payload: Any) -> str:
    """Best-effort format identification.

    Two of the artifacts this project consumes carry no format field at all
    (agent-eval-gate's ``run.json`` and DiscoverySpec's ``run-report.json``), so
    identification falls back to structural sniffing. The result is recorded in
    the envelope as an observation, never used to override the caller's declared
    kind.
    """
    if isinstance(payload, dict):
        for key in ("format", "contract_version", "policy", "map", "release", "scenario"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        if "results" in payload and "run_id" in payload:
            return "agent-eval-gate.run"
        if "scenarios" in payload and "verdict" in payload:
            return "discoveryspec.run-report"
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if "task_id" in payload[0] and "final_state_hash" in payload[0]:
            return "silobench.verify"
    return "unknown"


# ---------------------------------------------------------------------------
# per-kind parsers
# ---------------------------------------------------------------------------


CONTRACT_VERSION = "deployment-contract.v1"
ATTESTATION_BLOCK_KEYS = {
    "version",
    "kind",
    "algorithm",
    "public_key_fingerprint",
    "digest_sha256",
    "signature",
}
# A public-key fingerprint as attest.v1 emits it: the first 16 hex characters of
# the key's SHA-256.
_FINGERPRINT_HEX = re.compile(r"[0-9a-f]{16}")


def _is_well_formed_attestation(block: Any) -> bool:
    """True only for a block shaped like a real attest.v1 signature.

    Not a cryptographic check: verifying the signature needs the producer's public
    key, which this version does not collect, which is why the fact it feeds is
    named ``signature_present`` and not ``signature_verified``. What it refuses is
    a block that merely wears the shape: the six keys with null or arbitrary
    string values (version "bogus", algorithm "none", a one-character digest) that
    would otherwise satisfy a ``must: [signature_present]`` requirement with a
    value nobody signed. Each field must be the kind of value attest.v1 emits.
    """
    if not (isinstance(block, dict) and set(block) == ATTESTATION_BLOCK_KEYS):
        return False
    if not all(isinstance(block[k], str) and block[k].strip() for k in ATTESTATION_BLOCK_KEYS):
        return False
    if block["version"] != "attest.v1" or block["algorithm"] != "ed25519":
        return False
    if not SHA256_HEX.fullmatch(block["digest_sha256"]):
        return False
    if not _FINGERPRINT_HEX.fullmatch(block["public_key_fingerprint"]):
        return False
    try:
        raw = base64.b64decode(block["signature"], validate=True)
    except (ValueError, TypeError):
        return False
    # An ed25519 signature is exactly 64 bytes. Valid base64 of any other length
    # is not a signature this algorithm could have produced: the string
    # "fake-signature-for-fixture-use-only" is valid base64 and decodes to 35
    # bytes, and without this length gate it satisfied `must: [signature_present]`
    # as convincingly as a real one. Structural, not cryptographic: it proves the
    # block could carry an ed25519 signature, not that this one verifies.
    if len(raw) != 64:
        return False
    return True


def parse_contract(evidence_id: str, path: str, payload: Any, sha256: str) -> Reading:
    payload = _mapping(payload, path, "contract")

    # Identify the artifact before trusting anything in it. Without this, a
    # two-line JSON object with status "approved" and an empty signature block
    # is indistinguishable from a real approved contract, and its hash can then
    # be copied into a release descriptor to clear a release.
    version = payload.get("contract_version")
    if version != CONTRACT_VERSION:
        raise InputError(
            f"{path}: not a {CONTRACT_VERSION} document (contract_version is {version!r})"
        )

    metadata = _mapping(payload.get("metadata"), path, "contract metadata")
    missing = [k for k in ("project", "status") if k not in metadata]
    if missing:
        raise InputError(f"{path}: contract metadata is missing {missing}")

    status_field = metadata.get("status")
    if status_field not in ("draft", "approved"):
        raise InputError(f"{path}: contract status {status_field!r} is not draft or approved")

    signature = metadata.get("approval_signature")
    # The keys being present is not the signature being present, and neither is a
    # block of arbitrary strings. A block whose six keys are all null, or that
    # carries version "bogus" and algorithm "none" and a one-character digest, has
    # the shape of an attest.v1 block and none of its content; reporting it present
    # would satisfy a `must: [signature_present]` requirement with a value nobody
    # signed. Each field must carry a value of the kind attest.v1 actually emits:
    # the exact version and algorithm, a SHA-256 digest, a hex fingerprint, and a
    # base64 signature. Cryptographic verification still needs the producer's key,
    # which this version does not take, so this checks well-formedness, not a valid
    # signature, and it is named signature_present for exactly that reason.
    signature_shape_ok = _is_well_formed_attestation(signature)

    # Every requirement is validated, not filtered. `if isinstance(r, dict) and
    # r.get("id")` dropped anything it could not read, so an approved contract
    # could carry a requirement serialized as a bare string, or as an object with
    # no id, and coverage would then be computed over the requirements that
    # survived the filter and report every one of them covered. The contract's
    # own requirement count would no longer describe what was checked, and
    # nothing in the pack would say so.
    requirements = [
        _mapping(r, path, f"contract requirement {position}")
        for position, r in enumerate(
            _sequence(payload.get("requirements"), path, "contract requirements")
        )
    ]
    unnamed = [
        position
        for position, r in enumerate(requirements)
        if not (isinstance(r.get("id"), str) and r["id"].strip())
    ]
    if unnamed:
        raise InputError(
            f"{path}: contract requirement(s) at index {unnamed} carry no usable id, so "
            "they cannot be traced to any evidence and cannot be reported as uncovered"
        )
    req_ids = [r["id"] for r in requirements]
    repeated = sorted({rid for rid, count in Counter(req_ids).items() if count > 1})
    if repeated:
        # Two distinct requirements sharing an id collapse into one everywhere
        # downstream: the coverage rule reduces the list to a set, and the titles
        # map below keeps whichever requirement came last. One traceability entry
        # would then report both as covered, so the contract could promise two
        # things and be graded on one, with nothing in the pack saying so.
        raise InputError(
            f"{path}: contract requirement id(s) {repeated} appear more than once; distinct "
            "requirements sharing an id cannot be covered, traced, or reported separately"
        )

    approved = status_field == "approved"
    reading = Reading(
        evidence_id=evidence_id,
        kind="contract",
        path=path,
        status=Status.PASS if approved else Status.FAIL,
        detail=(
            "contract is approved"
            if approved
            else f"contract status is {status_field!r}, expected 'approved'"
        ),
        facts={
            "sha256": sha256,
            "project": metadata.get("project"),
            "transcript_sha256": metadata.get("transcript_sha256"),
            "approved_by": metadata.get("approved_by"),
            "approved_at": metadata.get("approved_at"),
            "signature_present": signature_shape_ok,
            "requirement_ids": req_ids,
            "requirement_titles": {r["id"]: r.get("title", "") for r in requirements},
            "slo": _mapping(payload.get("slo"), path, "contract slo"),
        },
    )
    if signature is not None and not signature_shape_ok:
        # An empty or partial block is not a signature. Accepting one because
        # the key exists would make `signature_present` mean "somebody typed the
        # word", which is worse than not checking at all.
        reading.problems.append(
            "contract carries an approval_signature that is not a well formed attest.v1 "
            "block; treating it as unsigned"
        )
    elif not signature_shape_ok:
        # `status: approved` is a structural claim anyone with a text editor can
        # make. Whether that blocks is the policy's decision via the
        # `signature_present` requirement on the evidence class.
        reading.problems.append(
            "contract carries no approval_signature; approved status is unauthenticated"
        )
    return reading


def _total_latency(steps: Any, path: str, scenario_id: str) -> Optional[float]:
    """Sum per-step latency, or None when the trajectory does not record it.

    ``sum`` over a raw JSON field aborts with a TypeError the moment a step
    carries ``"latency_s": "0.4"``, which would surface as a traceback and exit
    1, the code this tool reserves for a proven NO_GO. A non-finite value is
    left to pass through as the float it is, because the SLO rule is where a
    NaN has to be caught: refusing it here would report an unusable measurement
    as an unreadable artifact.

    Malformed nested data is refused rather than skipped. A ``steps`` field that
    is not an array, or a step that is not an object, used to contribute nothing
    and leave the total at zero, and zero is not an absent measurement: it is the
    fastest run imaginable, and it satisfies every latency ceiling a blocking SLO
    rule can declare. An unreadable trajectory has to read as unreadable.

    Absent timing is refused as a measurement for the same reason, and that is
    what None means here. A trajectory with no steps timed nothing, and a step
    that records no ``latency_s`` was not instantaneous; summing only the steps
    that carried a number reports a figure smaller than the run took and offers
    it as the measurement, which is the same claim in a quieter voice. None is
    not a number, so the SLO rule reads it as unusable and names the scenarios it
    could not measure instead of reporting a ceiling as met.
    """
    parsed = _sequence(steps, path, f"steps for scenario {scenario_id!r}")
    total = 0.0
    timed = 0
    for position, step in enumerate(parsed):
        if not isinstance(step, dict):
            raise InputError(
                f"{path}: scenario {scenario_id!r} records a step {position} that is not a "
                f"JSON object (got {type(step).__name__}), so its latency cannot be read"
            )
        value = step.get("latency_s")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError(
                f"{path}: scenario {scenario_id!r} records a step latency_s of "
                f"{type(value).__name__} {value!r}, which is not a number"
            )
        total += float(value)
        timed += 1
    if not parsed or timed < len(parsed):
        return None
    return total


# agent-eval-gate's run modes, as a CLOSED set, taken from ``RunReport`` in its
# models.py (``mode: Literal["live", "replay"]``) rather than inferred from a
# sample. Only the exact string "replay" turns on the two protections that read a
# run as historical: the SLO rule refuses to measure fixture latencies against a
# ceiling, and binding refuses to read a recorded model as evidence about this
# release. Every other value fell through to the live reading, so "bogus", null,
# false, and 0 each disarmed both checks while looking like a typo, and the pack
# recorded no disagreement because none was ever visible.
#
# An unrecognized mode is not a live run. It is a run that does not say whether
# its latencies and model ids were measured now or copied out of a fixture
# recorded at an unknown date, and a run whose provenance cannot be established
# cannot be graded as though it had been measured.
GATE_MODES = frozenset({"live", "replay"})

# The three answers this parser records for "does this result's trajectory belong
# to the scenario it is filed under", as a CLOSED set. Binding reads the flag
# rather than string-matching a message, and it has to read it as a closed set:
# the branch that clears a run claims every trajectory carries its own scenario
# id, and it is reached by the absence of two particular strings, so any third
# value would make that claim about a state nobody compared.
ID_BINDINGS = frozenset({"ok", "mismatch", "unverifiable"})


def parse_gate_run(evidence_id: str, path: str, payload: Any) -> Reading:
    """agent-eval-gate ``run.json``.

    The tool serializes no verdict: ``passed``, ``score``, and ``pass_rate`` are
    plain properties. Everything below is recomputed and recorded as derived.
    """
    payload = _mapping(payload, path, "gate run")

    results = _sequence(payload.get("results"), path, "gate run results")
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in GATE_MODES:
        # Refused rather than defaulted. `payload.get("mode", "live")` answered
        # on the producer's behalf whenever the producer did not, and the answer
        # it invented was the one that lets recorded latencies and model ids
        # stand as measurements of this release.
        raise InputError(
            f"{path}: gate run mode {mode!r} is outside the {sorted(GATE_MODES)} vocabulary "
            "this build interprets, so it cannot be established whether the recorded "
            "latencies and models measure this release or replay a fixture captured earlier"
        )
    # The run's top-level model header names the model the gate was configured
    # with, and binding compares it against the release. It is read as a string
    # there and skipped when it is not one, so a header of ``{}`` or ``42`` passed
    # every binding by being unreadable rather than by agreeing. A present header
    # that is not a string is a malformed run record, refused here rather than
    # carried forward to be silently ignored downstream. Absent (null) is allowed:
    # a run that did not record its header is handled by binding as unrecorded.
    agent_model = payload.get("agent_model")
    if agent_model is not None and not isinstance(agent_model, str):
        raise InputError(
            f"{path}: gate run agent_model is {type(agent_model).__name__}, not a string; a "
            "run header that is not a model name cannot be checked against the release"
        )
    scenarios: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    statuses: list[Status] = []
    # Names from a merged oracle namespace, collected as they are read. This was
    # computed afterwards by comparing `classify_check`'s reason against a string
    # that function does not return, so the disclosure below could never fire:
    # the checks were counted as behavioral assertions on another tool's word,
    # and the note recording that decision was absent from every pack ever built.
    oracle_names: set[str] = set()

    for index, result in enumerate(results):
        # Skipping a malformed entry would silently shrink the evidence set,
        # which is the failure mode this whole project exists to catch.
        result = _mapping(result, path, f"gate run result {index}")
        scenario_id = result.get("scenario_id", "")
        trajectory = _mapping(result.get("trajectory"), path, f"trajectory for result {index}")
        inner_id = trajectory.get("scenario_id", "")
        # Both ids are compared against each other, keyed into a dict, and sorted
        # beside the ids of every other scenario. A number or a null survives the
        # first two and aborts the third with a TypeError, and the sort it aborts
        # is the one that reports which scenarios named no model.
        for label, value in (
            ("scenario_id", scenario_id),
            ("trajectory scenario_id", inner_id),
        ):
            if not isinstance(value, str):
                raise InputError(
                    f"{path}: result {index} records a {label} of {type(value).__name__} "
                    f"{value!r}, which names no scenario this pack can trace or compare"
                )
        checks = [
            _mapping(c, path, f"check in result {index}")
            for c in _sequence(result.get("checks"), path, f"checks for result {index}")
        ]
        for position, entry in enumerate(checks):
            # A check name reaches `str.startswith` in the classifier below, so a
            # non-string name raised an AttributeError there: a crash on
            # malformed evidence, reported as an unexpected failure rather than
            # as the unreadable artifact it is. Refused rather than coerced,
            # because a name nobody can classify is a check nobody can count
            # either for or against the assertion-density rule.
            name = entry.get("name")
            if not isinstance(name, str):
                raise InputError(
                    f"{path}: check {position} in result {index} records a name of "
                    f"{type(name).__name__} {name!r}, so it can be classified neither as a "
                    "behavioral assertion nor as a harness health check"
                )
        judge = result.get("judge")
        # Read null-or-string, never by truthiness. The gate's own `derive_passed`
        # fails a scenario whenever `trajectory.error is not None`, so `false`,
        # `0`, and `{}` in that slot are malformed rather than "no error", and
        # `elif error:` would have read every one of them as a clean run.
        error = _optional_string(trajectory, "error", path, f"trajectory for result {index}")

        if scenario_id in scenarios:
            raise InputError(
                f"{path}: duplicate scenario_id {scenario_id!r}; one result would overwrite "
                "the other and its verdict would vanish"
            )

        # DISTINCT behavioral names, not a count of check objects. Two identical
        # `expected_tool:x` checks are one assertion asserted twice, and counting
        # them as two let a scenario reach a min_behavioral_checks of 2 while
        # pinning a single tool.
        behavioral = {
            c.get("name") for c in checks if classify_check(c.get("name", ""))[0]
        }
        failed = [
            c for c in checks
            if not _flag(c, "passed", path, f"check {c.get('name', '?')!r} in result {index}")
        ]
        judge_failed = []
        if judge is not None:
            judge_map = _mapping(judge, path, f"judge for result {index}")
            # Each criterion is validated, not filtered. `if isinstance(c, dict)`
            # dropped anything that was not an object, so a criterion serialized
            # as a bare string or a null was removed from the set being read and
            # the scenario passed on the remainder. A criterion nobody could read
            # is not a criterion nobody failed.
            criteria = [
                _mapping(c, path, f"judge criterion {position} for result {index}")
                for position, c in enumerate(
                    _sequence(judge_map.get("criteria"), path, f"judge criteria for result {index}")
                )
            ]
            judge_failed = [
                c
                for c in criteria
                if not _flag(
                    c, "passed", path, f"judge criterion {c.get('criterion', '?')!r}"
                )
            ]

        # Structured rather than inferred from the detail text later: binding
        # decisions must not depend on string-matching a human-readable message.
        if not scenario_id or not inner_id:
            id_binding = "unverifiable"
        elif inner_id != scenario_id:
            id_binding = "mismatch"
        else:
            id_binding = "ok"

        if scenario_id and not inner_id:
            # An absent inner id means the substitution check cannot run at all.
            # Skipping the check silently would be the worst option: it would
            # report PASS on precisely the artifact whose identity is
            # unverifiable.
            status = Status.INCONCLUSIVE
            detail = (
                f"result {scenario_id!r} carries a trajectory with no scenario id, so it "
                "cannot be confirmed to belong to this scenario"
            )
            problems.append(f"{scenario_id}: {detail}")
        elif inner_id and scenario_id and inner_id != scenario_id:
            # ReplayAdapter loads <fixtures>/<scenario_id>.json and returns the
            # parsed trajectory without checking the id inside it, so a fixture
            # filed under the wrong name is accepted silently upstream.
            status = Status.INCONCLUSIVE
            detail = (
                f"fixture substitution: result is filed as {scenario_id!r} but the "
                f"trajectory inside it is {inner_id!r}"
            )
            problems.append(f"{scenario_id}: {detail}")
        elif error is not None:
            # A harness error collapses the checks array to [no_harness_error],
            # so the absence of other failures here means nothing.
            status = Status.INCONCLUSIVE
            detail = f"harness error: {error}"
        elif failed or judge_failed:
            status = Status.FAIL
            names = [c.get("name", "?") for c in failed] + [
                c.get("criterion", "?") for c in judge_failed
            ]
            detail = "failed: " + ", ".join(names)
        elif not behavioral:
            status = Status.INCONCLUSIVE
            detail = (
                f"no behavioral assertion: {len(checks)} check(s) present but all are "
                "harness health or resource guards"
            )
            problems.append(f"{scenario_id}: scenario asserts nothing about agent behaviour")
        else:
            status = Status.PASS
            detail = f"{len(behavioral)} behavioral check(s) passed"

        oracle_names.update(
            name for c in checks if (name := c.get("name", "")).startswith(ORACLE_PREFIXES)
        )
        # Only a non-empty string names a model. Absent, null, and a number are
        # all a scenario that did not say, and they are recorded as such rather
        # than carried into the comparison below, where a non-string would abort
        # the sort with a TypeError.
        model = trajectory.get("model", "")
        cost_usd = result.get("cost_usd")
        # Passed straight from the trajectory rather than through `or []`: that
        # idiom turned every falsy value, including an object or an empty string
        # where an array belongs, into an empty step list and a zero total.
        latency_s = _total_latency(trajectory.get("steps"), path, scenario_id)
        # A non-finite cost or latency is not a measurement, and it must not clear
        # a run. `json.loads` accepts NaN and Infinity, `_total_latency` carries a
        # NaN through by design so the SLO rule can see it, and cost_usd is read
        # raw; but when the contract declares no SLO the metric is never read
        # again, so a run reporting `latency_s: NaN` cleared every ceiling because
        # nothing looked. Refused here, unconditionally, the run reads as
        # unreadable rather than infinitely fast or free.
        nonfinite = [
            name
            for name, value in (("latency_s", latency_s), ("cost_usd", cost_usd))
            if isinstance(value, float) and not math.isfinite(value)
        ]
        if nonfinite:
            status = worst([status, Status.INCONCLUSIVE])
            detail = (
                f"{scenario_id}: {', '.join(nonfinite)} is not a finite number, so this "
                "run's resource use is unreadable and cannot clear a ceiling"
            )
            problems.append(detail)
        scenarios[scenario_id] = {
            "status": status.value,
            "id_binding": id_binding,
            "behavioral_checks": len(behavioral),
            "total_checks": len(checks),
            "model": model if isinstance(model, str) else "",
            "cost_usd": cost_usd,
            "latency_s": latency_s,
            "detail": detail,
        }
        statuses.append(status)

    reading = Reading(
        evidence_id=evidence_id,
        kind="gate-run",
        path=path,
        status=worst(statuses),
        detail=f"{len(statuses)} scenario(s), mode {mode}",
        facts={
            "mode": mode,
            "agent_model_field": payload.get("agent_model", ""),
            "scenarios": scenarios,
            # In replay mode `agent_model` is the literal string "replay"; the
            # real model id only appears per scenario.
            "models": sorted({s["model"] for s in scenarios.values() if s["model"]}),
            # The scenarios that named no model, carried beside the set rather
            # than only filtered out of it. The filter alone made `models`
            # describe the scenarios that answered, so a run where one scenario
            # named the declared model and another named nothing bound as "all
            # scenarios ran on <declared>": a claim about scenarios that said
            # nothing. Binding needs to see them to refuse that.
            "scenarios_without_model": sorted(
                scenario_id for scenario_id, info in scenarios.items() if not info["model"]
            ),
            "run_id": payload.get("run_id", ""),
        },
        problems=problems,
        derivations=[
            "per-scenario pass recomputed from checks, judge criteria, and trajectory.error "
            "(agent-eval-gate serializes no verdict)",
            "behavioral check counts derived from the check-name allowlist",
        ],
    )
    if oracle_names:
        reading.derivations.append(
            "treated as external oracle results (counted as behavioral): "
            + ", ".join(sorted(oracle_names))
        )
    if mode == "replay":
        reading.derivations.append(
            "replay run: recorded latencies are historical fixture values, not "
            "measurements of this release"
        )
    return reading


STATEDIFF_FORMAT = "statediff.verdict.v1"

# StateDiff's per-check vocabulary, as a CLOSED set, taken from CheckOutcome in
# its models.py rather than inferred from a sample. `not_applicable` is a real
# third state there: an allowed effect that did not fire justified nothing, so
# calling it a pass would claim a predicate held when it did not. It is a known
# status and it does not block, which is what StateDiff's own aggregate does.
# Anything outside this set is a status this build cannot interpret.
STATEDIFF_CHECK_STATUSES = frozenset({"pass", "fail", "not_applicable"})


def parse_state_verdict(evidence_id: str, path: str, payload: Any) -> Reading:
    # Structural gate first: strict types, non-empty ids, a string status on every
    # check, a modeled artifacts/provenance shape. Everything the hand-checks
    # below used to catch one field at a time, refused in one place, before any
    # verdict logic runs. The semantic contradictions (a pass beside an error, a
    # failing check, or an unexplained change) are still normalized to
    # INCONCLUSIVE below rather than refused, because those are a producer
    # disagreeing with itself, not an unreadable artifact.
    validate_producer(StateDiffVerdict, payload, path, STATEDIFF_FORMAT)
    payload = _mapping(payload, path, "state verdict")

    declared = payload.get("format")
    if declared != STATEDIFF_FORMAT:
        raise InputError(f"{path}: not a {STATEDIFF_FORMAT} document (format is {declared!r})")

    raw = payload.get("status")
    mapping = {"pass": Status.PASS, "fail": Status.FAIL, "error": Status.INCONCLUSIVE}
    if raw not in mapping:
        raise InputError(f"{path}: unknown statediff status {raw!r}")

    # The top-level error is a sibling of the status: StateDiff's Verdict model
    # types it `str | None` and fills it only when it could not grade the world
    # state. It used to be read solely as display text, so a `pass` sitting
    # beside a non-null error cleared the release with the failure printed one
    # field away. Read as null-or-string so `false`/`0`/`{}` in that slot cannot
    # read as no error either.
    error = _optional_string(payload, "error", path, "state verdict")

    provenance = payload.get("provenance")
    if provenance is not None:
        provenance = _mapping(provenance, path, "state verdict provenance")
    artifacts = _mapping(payload.get("artifacts"), path, "state verdict artifacts")
    scenario_id = payload.get("scenario_id", "")
    # Both ids become set members and dict keys downstream: the scenario id in
    # the statediff namespace of `_executing_ids`, the task id keying the
    # provenance claims the conflict rule compares. A number would silently miss
    # every match; a list is unhashable and would abort those with a TypeError,
    # a crash on malformed evidence rather than the exit 2 this tool promises.
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise InputError(
            f"{path}: state verdict scenario_id is {type(scenario_id).__name__} "
            f"{scenario_id!r}, which names no scenario this pack can trace or compare"
        )
    silobench_task = (provenance or {}).get("silobench_task")
    if silobench_task is not None and not isinstance(silobench_task, str):
        raise InputError(
            f"{path}: state verdict provenance silobench_task is "
            f"{type(silobench_task).__name__} {silobench_task!r}, which names no task this "
            "pack can trace or compare"
        )

    checks = [
        _mapping(c, path, "state verdict check")
        for c in _sequence(payload.get("checks"), path, "state verdict checks")
    ]
    # Every check must name a predicate with a non-empty id and carry a string
    # status. A check with a recognized status but no id establishes nothing
    # identifiable, yet a top-level `pass` over such checks satisfied the required
    # state class and the blocking state rule. And a status that is not a string
    # (a list, say) is unhashable, so the membership test below raised a TypeError
    # and crashed to an exit-1 traceback rather than the clean exit-2 refusal.
    for position, check in enumerate(checks):
        cid = check.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise InputError(
                f"{path}: state verdict check {position} names no id, so it asserts "
                "nothing identifiable about the world state"
            )
        if not isinstance(check.get("status"), str):
            raise InputError(
                f"{path}: state verdict check {cid!r} has a non-string status "
                f"{check.get('status')!r}, which evidences neither success nor failure"
            )
    failing = [c.get("id") for c in checks if c.get("status") == "fail"]
    # The checks that report a predicate actually held, counted rather than
    # inferred from the absence of failures. StateDiff computes its own aggregate
    # as "nothing failed", and an empty check list satisfies that, as does a list
    # in which every check is `not_applicable`: an allowed effect that never
    # fired justified nothing. Both shapes produced a top-level `pass` that
    # asserted nothing about the world state, and that pass then satisfied the
    # required state-evidence class and the blocking state rule at once.
    passing = [c.get("id") for c in checks if c.get("status") == "pass"]
    # Membership of a known vocabulary, not inequality against one string.
    # Reading "anything that is not the word fail" as a pass means a check whose
    # status this build cannot interpret, whether it is a new state upstream, a
    # misspelling, or a value somebody wrote by hand, silently counts as a check
    # that succeeded. An unrecognized status is not evidence of success; it is
    # the absence of any evidence at all.
    unreadable = [
        f"{c.get('id')} ({c.get('status')!r})"
        for c in checks
        if c.get("status") not in STATEDIFF_CHECK_STATUSES
    ]

    status = mapping[raw]
    consistency: str | None = None
    if raw == "pass" and error is not None:
        # A producer that recorded an error has not reported a pass. Checked
        # ahead of the per-check contradictions because it is the verdict's own
        # top-level signal disagreeing with its status, and it holds even when
        # the check list is empty.
        status = Status.INCONCLUSIVE
        consistency = (
            f"verdict claims 'pass' while carrying a non-null error ({error!r}); a producer "
            "that recorded an error has not reported a pass"
        )
    elif raw == "pass" and failing:
        # The top-level status and the per-check results contradict each other.
        # Trusting the summary would let a doctored header hide real failures,
        # and trusting the checks would override the producer. Refusing to read
        # a self-contradictory artifact is the only honest option.
        status = Status.INCONCLUSIVE
        consistency = (
            f"verdict claims 'pass' while {len(failing)} check(s) are marked fail "
            f"({', '.join(str(f) for f in failing[:5])}); the artifact contradicts itself"
        )
    elif raw == "pass" and not passing:
        # A verdict that asserted nothing is not a verdict that passed. Reading
        # it as one lets a scenario whose checks were all skipped stand in for
        # evidence that the world state was graded, which is the whole reason a
        # state oracle is in the pack.
        status = Status.INCONCLUSIVE
        consistency = (
            f"verdict claims 'pass' but none of its {len(checks)} check(s) reports one, so "
            "no state predicate was established; the top-level status records only that "
            "nothing failed"
        )
    elif raw == "pass" and (payload.get("unexplained") or []):
        # StateDiff's unexplained sweep records state changes that no expected or
        # allowed effect justifies. A pass beside a non-empty unexplained set
        # contradicts itself: the world changed in ways the spec did not account
        # for, which is precisely what a state oracle exists to catch. Read only
        # the per-check statuses, this array was dead evidence.
        unexplained = payload.get("unexplained") or []
        status = Status.INCONCLUSIVE
        consistency = (
            f"verdict claims 'pass' while recording {len(unexplained)} unexplained state "
            "change(s); changes the spec does not account for are not a pass"
        )

    reading = Reading(
        evidence_id=evidence_id,
        kind="state-verdict",
        path=path,
        status=status,
        detail=error or f"scenario {scenario_id}: {raw}",
        facts={
            "scenario_id": scenario_id,
            "before_state_hash": artifacts.get("before_state_hash"),
            "after_state_hash": artifacts.get("after_state_hash"),
            # The environment the verdict was graded under. Bound in binding.py:
            # a matching before-state hash proves a common seeded start, but six
            # of the twelve golden hashes are identical, so a verdict declaring
            # one schema can share another release's initial hash and bind on it
            # alone unless the schema is compared too.
            "schema_version": artifacts.get("schema_version"),
            # Read as arrays of identifiers rather than through `list(... or [])`:
            # that idiom turns a field serialized as a single string into the
            # list of its characters, and the requirement list is one of the
            # three independent opinions the conflict rule compares.
            "requirements": _string_list(
                (provenance or {}).get("requirements"), path, "state verdict requirements"
            ),
            "turns": _string_list((provenance or {}).get("turns"), path, "state verdict turns"),
            "silobench_task": silobench_task,
            "failed_checks": failing,
        },
    )
    if consistency:
        reading.problems.append(consistency)
    if unreadable:
        # `worst` rather than an assignment, so a verdict that already reads FAIL
        # is not lifted to INCONCLUSIVE by an unreadable check beside it.
        reading.status = worst([reading.status, Status.INCONCLUSIVE])
        reading.problems.append(
            f"{len(unreadable)} check(s) carry a status this build does not recognize "
            f"({', '.join(unreadable[:5])}), so they evidence neither success nor failure"
        )
    if raw == "error" and provenance is None:
        # On a scenario load failure statediff sets scenario_id to the filename
        # stem, so the id is not a real scenario identifier here.
        reading.problems.append(
            f"scenario failed to load; scenario_id {scenario_id!r} is a filename stem, "
            "not a scenario identifier"
        )
    return reading


CANARY_FORMAT = "blast-radius.v1"

# The canary's severity vocabulary, as a CLOSED set, matching SEVERITY_RANK in
# policy.py. The rule that enforces a ceiling counts these three names and
# nothing else, so a severity outside the set is counted nowhere: a `clean`
# report carrying a group marked `catastrophic` cleared the ceiling by being
# unrecognized. A severity this build cannot rank is a severity it cannot
# compare against a ceiling, and refusing to read the artifact is the only
# answer that does not silently drop the group.
CANARY_SEVERITIES = frozenset({"info", "risky", "breaking"})


def parse_blast_radius(evidence_id: str, path: str, payload: Any) -> Reading:
    payload = _mapping(payload, path, "blast radius report")

    declared = payload.get("format")
    if declared != CANARY_FORMAT:
        raise InputError(f"{path}: not a {CANARY_FORMAT} document (format is {declared!r})")

    verdict = _mapping(payload.get("verdict"), path, "canary verdict")
    state = verdict.get("state")
    mapping = {
        "clean": Status.PASS,
        "breaking": Status.FAIL,
        # Coverage gaps mean unknown, not broken. Under the decision precedence
        # an inconclusive required class still cannot clear a release.
        "clean-with-gaps": Status.INCONCLUSIVE,
        "error": Status.INCONCLUSIVE,
    }
    if not isinstance(state, str) or state not in mapping:
        raise InputError(f"{path}: unknown canary verdict state {state!r}")

    groups = [
        _mapping(g, path, f"change group {position}")
        for position, g in enumerate(
            _sequence(payload.get("change_groups"), path, "change_groups")
        )
    ]
    severities = [g.get("severity") for g in groups]
    # `not in` on a frozenset hashes its left operand, so a severity that is not a
    # string (a list, say) used to raise TypeError and crash to an exit-1
    # traceback instead of a clean exit-2 refusal. A non-string severity is
    # unrankable, which is exactly what this rejects.
    unranked = sorted({str(s) for s in severities
                       if not isinstance(s, str) or s not in CANARY_SEVERITIES})
    if unranked:
        raise InputError(
            f"{path}: change group severit(ies) {unranked} are outside the "
            f"{sorted(CANARY_SEVERITIES)} vocabulary this build ranks, so they cannot be "
            "compared against a policy ceiling"
        )

    # Every affected entry is validated, not filtered. `if isinstance(a, dict)
    # and a.get("scenario_id")` dropped anything it could not read, and what it
    # dropped is the canary's opinion about which requirements a scenario
    # exercises: one malformed entry removed that opinion from the map, the
    # conflict rule then saw fewer than two independent sources for the scenario,
    # and two producers stopped disagreeing without either of them changing its
    # mind. An entry naming no scenario is refused for the same reason as an
    # unnamed contract requirement: its claims attach to nothing, so they can
    # neither be compared nor reported as uncompared.
    affected = [
        _mapping(a, path, f"affected entry {position}")
        for position, a in enumerate(_sequence(payload.get("affected"), path, "affected"))
    ]
    unnamed = [
        position
        for position, a in enumerate(affected)
        if not (isinstance(a.get("scenario_id"), str) and a["scenario_id"].strip())
    ]
    if unnamed:
        raise InputError(
            f"{path}: affected entr(ies) at index {unnamed} name no scenario, so the "
            "requirement claims they carry cannot be compared against any other producer's "
            "opinion about the same scenario"
        )
    # Built with an explicit duplicate check rather than a dict comprehension. A
    # comprehension collapsed a repeated scenario_id last-wins, so an earlier
    # entry's requirement claim was silently overwritten by a later one and the
    # conflict rule compared a claim set one opinion short: two producers stopped
    # disagreeing without either changing its mind. Duplicate JSON and YAML keys
    # are already a hard error everywhere evidence is read, and a repeated
    # affected scenario is the same rewrite spelled inside a list.
    affected_map: dict[str, list[str]] = {}
    for a in affected:
        scenario_id = a["scenario_id"]
        if scenario_id in affected_map:
            raise InputError(
                f"{path}: duplicate affected scenario_id {scenario_id!r}; one entry would "
                "overwrite the other and its requirement claims would vanish"
            )
        affected_map[scenario_id] = sorted(
            _string_list(a.get("requirement_ids"), path, f"requirement_ids for {scenario_id}")
        )

    status = mapping[state]
    problems: list[str] = []

    # The canary chooses `clean` from contract-change severity alone; a targeted
    # replay can fail without moving that state. Reading only `verdict.state`
    # would report a run with failed replays as passing.
    #
    # `verdict.get("hard_failures") or 0` supplied that answer whenever the
    # report did not: a `clean` report naming no counters at all read as zero
    # failures, so the check cleared having verified nothing, which is the one
    # shape it exists to catch. The counters are demanded only where they can
    # change the answer, because under any other state this check cannot lift the
    # status and a report that already fails should not be refused over a number
    # nobody would read.
    if status is Status.PASS:
        # `safe` is the producer's own top-level go/no-go, required here for the
        # same reason as the counters: it can change the answer only on a report
        # that would otherwise clear, so it is demanded where it decides and not
        # where a report already fails. `verdict.get("safe", False)` used to copy
        # it into the facts and evaluate it nowhere, so a `clean` report that
        # flagged itself unsafe, or omitted the flag entirely, still cleared the
        # release. A safety signal the producer sets and the consumer ignores is
        # decoration.
        safe = _flag(verdict, "safe", path, "canary verdict")
        hard_failures = _count(verdict, "hard_failures", path, "canary verdict")
        state_failures = _count(verdict, "state_failures", path, "canary verdict")
        # Recomputed from the replay RESULTS, not trusted from the summary counters
        # a forger controls. A `clean, safe` report can set hard_failures and
        # state_failures to zero while replay.results still record failed replays,
        # so the counters are believed only where the results do not contradict
        # them: the actual failures are read off the results and the larger of the
        # two wins.
        replay_block = payload.get("replay")
        replay_block = replay_block if isinstance(replay_block, dict) else {}
        replay_results = replay_block.get("results")
        replay_results = replay_results if isinstance(replay_results, list) else []
        def _replay_result_passed(r: Any) -> bool:
            # A replay result clears only when it says so in the affirmative: a
            # dict whose derived_passed is the boolean True, carrying no failed
            # checks and no harness error. Counting only the results that
            # explicitly declared failure was the hole: an empty ``{}`` records no
            # verdict, and ``derived_passed: "false"`` is a non-empty string that
            # ``is False`` never matches, so both slipped through as non-failures
            # and a report full of them stayed clean. ``is True`` because a truthy
            # test would read the string "false" as passing.
            return (
                isinstance(r, dict)
                and r.get("derived_passed") is True
                and not r.get("failed_checks")
                and not r.get("harness_error")
            )

        result_failures = sum(1 for r in replay_results if not _replay_result_passed(r))
        result_state_failures = sum(
            1 for r in replay_results if isinstance(r, dict) and r.get("state_check_failures")
        )
        hard_failures = max(hard_failures, result_failures)
        state_failures = max(state_failures, result_state_failures)
        breaking = severities.count("breaking")
        risky = severities.count("risky")
        if breaking or risky:
            # A clean report cannot carry a breaking or risky change group. The
            # canary derives `clean` from contract severity, so a group of that
            # severity sitting beside a clean state is a forged or malformed report
            # whose label contradicts its own contents. Grade the groups this
            # parser actually read, not the state field a forger controls.
            status = Status.FAIL
            problems.append(
                f"canary state is {state!r} but the report lists {breaking} breaking and "
                f"{risky} risky change group(s); the state field contradicts the change groups"
            )
        elif hard_failures or state_failures:
            status = Status.FAIL
            problems.append(
                f"canary state is {state!r} but the report records {hard_failures} hard and "
                f"{state_failures} state replay failure(s); the state field reflects contract "
                "severity only"
            )
        elif not safe:
            # The producer's own safety verdict disagrees with the clean state it
            # derived from contract severity. Which side is right is not something
            # this build can adjudicate, so it reports the disagreement rather
            # than clearing the release on the state field alone.
            status = Status.INCONCLUSIVE
            problems.append(
                f"canary state is {state!r} but the report's own verdict.safe is false, so "
                "the producer's top-level safety signal contradicts the clean state it derived"
            )

    return Reading(
        evidence_id=evidence_id,
        kind="blast-radius",
        path=path,
        status=status,
        detail=f"canary verdict {state}, {len(groups)} change group(s)",
        problems=problems,
        facts={
            "state": state,
            # No `, False` default: absent is recorded as absent, never as a
            # positive claim of unsafe the producer did not make. On a clean
            # report the flag is validated and evaluated above.
            "safe": verdict.get("safe"),
            # Read through `_mapping`, not `payload.get(...) or {}`: a side
            # serialized as a string reached `.get` and raised an AttributeError,
            # which surfaces as a traceback rather than as the unreadable
            # artifact it is.
            "baseline_fingerprint": _mapping(
                payload.get("baseline"), path, "canary baseline"
            ).get("fingerprint"),
            "candidate_fingerprint": _mapping(
                payload.get("candidate"), path, "canary candidate"
            ).get("fingerprint"),
            "severities": severities,
            "breaking": severities.count("breaking"),
            "risky": severities.count("risky"),
            "info": severities.count("info"),
            "affected_scenarios": sorted(affected_map),
            # Kept per scenario, not flattened. The canary's consumer map is one
            # of the three independent opinions about which requirement a task
            # exercises, and flattening it would destroy exactly the association
            # the conflict rule compares.
            "affected_map": affected_map,
        },
    )


def parse_silobench_verify(evidence_id: str, path: str, payload: Any) -> Reading:
    """SiloBench ``verify --json``: an array of TaskRunResult.

    ``verdict.passed`` covers the checker assertions only. The golden state-hash
    comparison is a separate concern and is applied in ``cross_reference``,
    because it needs the committed golden file, which is separate evidence.
    """
    entries = _sequence(payload, path, "silobench verify output")

    tasks: dict[str, dict[str, Any]] = {}
    profiles: list[dict[str, Any]] = []
    outages: list[bool] = []
    problems: list[str] = []
    for index, entry in enumerate(entries):
        entry = _mapping(entry, path, f"silobench verify entry {index}")
        task_id = entry.get("task_id", "")
        # Keyed into `tasks` and matched against the golden and traceability
        # namespaces. A non-string names no task there, and a list is unhashable
        # and would abort the dict insertion with a TypeError.
        if not isinstance(task_id, str) or not task_id.strip():
            raise InputError(
                f"{path}: verify entry {index} records a task_id of "
                f"{type(task_id).__name__} {task_id!r}, which names no task this pack can "
                "trace or compare"
            )
        if task_id in tasks:
            # Later entries would overwrite earlier ones in the dict, so a
            # failing result followed by a passing duplicate would silently
            # become a pass.
            raise InputError(
                f"{path}: duplicate task_id {task_id!r}; one result would overwrite the other"
            )
        verdict = _mapping(entry.get("verdict"), path, f"verdict for entry {index}")
        profile = _mapping(entry.get("profile"), path, f"profile for entry {index}")
        passed = _flag(verdict, "passed", path, f"verdict for entry {index}")
        # A task that records no profile records no environment, and an empty one
        # used to be waved through: `if profile` skipped the outage read, and the
        # `if p` filter on `schema_versions` below dropped it from the set
        # binding compares. A run of two tasks where only one carried a profile
        # therefore bound cleanly for both, on the strength of the task that
        # answered, while the other task's environment was never established.
        # Every task in a verify run has to say where it ran.
        if not profile:
            raise InputError(
                f"{path}: verify entry {index} ({task_id!r}) records no profile, so the "
                "schema version and outage state it ran under cannot be established"
            )
        schema_version = profile.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise InputError(
                f"{path}: verify entry {index} ({task_id!r}) records a profile schema_version "
                f"of {type(schema_version).__name__} {schema_version!r}, which names no "
                "environment this release can be bound to"
            )
        profiles.append(profile)
        # A profile must say what it ran under. `bool(...)` was a truthiness
        # test, so a profile recording the string "false" read as an outage in
        # progress, and a run captured under the wrong docs profile then bound
        # cleanly to a release declaring the opposite. There is no safe coercion:
        # "false" and "true" are equally truthy.
        docs_outage = _flag(profile, "docs_outage", path, f"profile for entry {index}")
        outages.append(docs_outage)
        # Each failure entry is validated rather than filtered. Dropping the ones
        # that are not objects would shrink the array the contradiction check
        # below reads, so a single malformed entry would restore the exact
        # `passed: true` with unreported failures this refuses.
        failures = [
            _mapping(f, path, f"failure {position} in verdict for entry {index}")
            for position, f in enumerate(
                _sequence(verdict.get("failures"), path, "verdict failures")
            )
        ]

        # SiloBench's top-level `failure` is a FailureInfo|null the runner sets
        # when the scripted reference itself raised, and its own runner forces
        # `passed: false` whenever it is present. The parser read `verdict.passed`
        # and `verdict.failures` but never this sibling, so a doctored entry
        # pairing `passed: true` with a recorded `failure` kept its pass with the
        # runner crash sitting one key away. A task that recorded a failure has
        # not reported a pass; this is the StateDiff error sibling in a different
        # producer. `is not None` so a falsy failure value is not read as silence.
        failure = entry.get("failure")

        status = Status.PASS if passed else Status.FAIL
        if passed and failure is not None:
            status = Status.INCONCLUSIVE
            problems.append(
                f"{task_id}: verdict claims passed while the task records a top-level failure "
                f"({failure!r}); a task that recorded a failure has not reported a pass"
            )
        if passed and failures:
            # The producer contradicts itself: the verdict claims the checker
            # passed while the same verdict lists checks that failed. Believing
            # `passed` would let a doctored or buggy aggregate hide real
            # failures, and believing the array would override the producer on
            # its own result. Neither can be established, so neither is claimed.
            status = Status.INCONCLUSIVE
            named = ", ".join(str(f.get("check")) for f in failures[:5])
            problems.append(
                f"{task_id}: verdict claims passed while listing {len(failures)} "
                f"failure(s) ({named}); the artifact contradicts itself"
            )

        tasks[task_id] = {
            "passed": passed,
            "is_default_run": _optional_flag(
                entry, "is_default_run", path, f"verify entry {index}"
            ),
            "final_state_hash": entry.get("final_state_hash"),
            "schema_version": profile.get("schema_version"),
            "docs_outage": docs_outage,
            "failures": [f.get("check") for f in failures],
            # Provisional: cross_reference may downgrade this to FAIL on a
            # golden mismatch.
            "status": status.value,
        }

    return Reading(
        evidence_id=evidence_id,
        kind="silobench-verify",
        path=path,
        status=worst([Status(t["status"]) for t in tasks.values()]),
        detail=f"{len(tasks)} task(s) verified",
        problems=problems,
        facts={
            "tasks": tasks,
            # Surfaced for release binding: a run under a different schema
            # version or outage profile describes a different environment. Every
            # collected profile is in the set, because one that is left out is
            # one the release is never compared against.
            "schema_versions": sorted({p["schema_version"] for p in profiles}),
            "docs_outages": sorted(set(outages)),
        },
        derivations=["golden state-hash comparison applied in cross-reference"],
    )


GOLDEN_FORMAT = "silobench-golden.v1"


def parse_environment_golden(evidence_id: str, path: str, payload: Any) -> Reading:
    payload = _mapping(payload, path, "golden hashes")

    # Identify the artifact before any release is compared against it. Nothing
    # else does: a two-line JSON object was a committed golden, and the only
    # thing the comparison downstream performs is an equality test between this
    # file and a SiloBench run. Neither side was required to look like a hash, so
    # a golden recording `"TASK-10": "x"` matched a run reporting `"x"` and the
    # pack then said the final world state matched the committed golden.
    declared = payload.get("format")
    if declared != GOLDEN_FORMAT:
        raise InputError(f"{path}: not a {GOLDEN_FORMAT} document (format is {declared!r})")

    tasks = _mapping(payload.get("tasks"), path, "golden tasks")
    unusable = sorted(
        f"{task_id} ({value!r})"
        for task_id, value in tasks.items()
        if not (isinstance(value, str) and SHA256_HEX.fullmatch(value))
    )
    if unusable:
        # A golden hash is the value a final world state is measured against, so
        # one that is not a digest measures nothing: it can only equal a run that
        # reports the same non-hash, and two matching placeholders corroborate
        # each other rather than the release.
        raise InputError(
            f"{path}: golden task hash(es) {unusable} are not SHA-256 digests, so no final "
            "state can be compared against them"
        )

    return Reading(
        evidence_id=evidence_id,
        kind="environment-golden",
        path=path,
        # Reference data asserts nothing about this release. NOT_APPLICABLE sits
        # above INCONCLUSIVE in the lattice, so a class of reference data does
        # not block a decision the way missing evidence does, while still never
        # being mistaken for a passing test.
        status=Status.NOT_APPLICABLE,
        detail="reference data: golden hashes, carries no verdict",
        facts={
            "initial": _mapping(payload.get("initial"), path, "golden initial"),
            "tasks": tasks,
        },
    )


def parse_opaque(evidence_id: str, kind: str, path: str, payload: Any, sha256: str = "") -> Reading:
    return Reading(
        evidence_id=evidence_id,
        kind=kind,
        path=path,
        status=Status.NOT_APPLICABLE,
        detail=f"{kind}: present and well formed, carries no verdict",
        # Carried so binding can pin transcript bytes against the contract's
        # recorded transcript hash. Without it the transcript is unbindable
        # content that still satisfies an evidence class.
        facts={"sha256": sha256},
    )


_PARSERS = {
    "contract": lambda eid, path, payload, sha: parse_contract(eid, path, payload, sha),
    "gate-run": lambda eid, path, payload, sha: parse_gate_run(eid, path, payload),
    "state-verdict": lambda eid, path, payload, sha: parse_state_verdict(eid, path, payload),
    "blast-radius": lambda eid, path, payload, sha: parse_blast_radius(eid, path, payload),
    "silobench-verify": lambda eid, path, payload, sha: parse_silobench_verify(eid, path, payload),
    "environment-golden": lambda eid, path, payload, sha: parse_environment_golden(
        eid, path, payload
    ),
}


def parse(evidence_id: str, kind: str, path: str | Path, sha256: str) -> Reading:
    text_kinds = {"transcript"}
    if kind in text_kinds:
        return parse_opaque(evidence_id, kind, str(path), None, sha256)
    payload = load_json(path)
    handler = _PARSERS.get(kind)
    if handler is None:
        return parse_opaque(evidence_id, kind, str(path), payload, sha256)
    return handler(evidence_id, str(path), payload, sha256)


def _merge_goldens(goldens: list[Reading]) -> tuple[dict[str, Any], set[str]]:
    """Merge every collected golden, and name the tasks they disagree about.

    Reading only the first ``environment-golden`` artifact made the answer a
    function of collection order: two committed goldens recording different
    hashes for one task would both bind, and whichever ``collect`` happened to
    receive first decided what the release was checked against. Contradictory
    reference data is not reference data, and picking a winner from argument
    order is the one resolution that cannot be defended.
    """
    merged: dict[str, Any] = {}
    contradictory: set[str] = set()
    for reading in goldens:
        for task_id, expected in (reading.facts.get("tasks") or {}).items():
            if task_id in merged and merged[task_id] != expected:
                contradictory.add(task_id)
            merged.setdefault(task_id, expected)
    return merged, contradictory


def cross_reference(readings: list[Reading], non_default_runs_expected: bool = False) -> None:
    """Apply checks that need more than one artifact. Mutates in place.

    Currently one: SiloBench reports ``verdict.passed`` from its checker only,
    and computes ``final_state_hash`` separately. A task whose assertions pass
    while its final world state diverges from the committed golden is a real
    release failure that ``verdict.passed`` alone reports as success.

    ``non_default_runs_expected`` carries the release descriptor's declaration
    that this release runs non-default SiloBench profiles on purpose. It
    defaults to False because the strict reading is the safe one: a task the
    golden cannot be applied to has had no final state established, and only an
    operator can say that was intended.
    """
    # Cross-artifact final-state consistency, run before the golden comparison so
    # it factors into every silobench task status. A StateDiff verdict and a
    # SiloBench verify for the same task should name the same final world, so
    # StateDiff's artifacts.after_state_hash and SiloBench's final_state_hash are
    # compared. There is no shared run id between the two producers, so a
    # disagreement is not proof the same execution ended two ways; it is proof the
    # two artifacts cannot be bound to a single final world. That is INCONCLUSIVE
    # (the release goes INCOMPLETE), not FAIL: the honest reading of unbindable
    # evidence, and it still refuses to clear.
    statediff_after_all: dict[str, list[tuple[Reading, str]]] = {}
    for reading in readings:
        if reading.kind == "state-verdict":
            task = reading.facts.get("silobench_task")
            after = reading.facts.get("after_state_hash")
            if task and isinstance(after, str) and after:
                statediff_after_all.setdefault(task, []).append((reading, after))
    # Two StateDiff verdicts for one task that disagree on the after-state leave no
    # single established final world. Keying a dict on the task silently kept
    # whichever verdict was read last and dropped the contradiction; here every
    # verdict for such a task is marked INCONCLUSIVE and the disagreement recorded,
    # and the task is excluded from the SiloBench comparison because there is no
    # single after-state to compare against.
    sd_contradictory: set[str] = set()
    statediff_after: dict[str, tuple[Reading, str]] = {}
    for task, entries in statediff_after_all.items():
        distinct = {h for _, h in entries}
        if len(distinct) > 1:
            sd_contradictory.add(task)
            for sd_reading, _ in entries:
                sd_reading.status = worst([sd_reading.status, Status.INCONCLUSIVE])
                sd_reading.problems.append(
                    f"{task}: StateDiff verdicts record different after-states "
                    f"({sorted(distinct)}) for the same task, so no single final world state "
                    "is established for it"
                )
        else:
            statediff_after[task] = entries[0]
    for reading in readings:
        if reading.kind != "silobench-verify":
            continue
        for task_id, info in (reading.facts.get("tasks") or {}).items():
            final = info.get("final_state_hash")
            if task_id in sd_contradictory and isinstance(final, str) and final:
                info["status"] = worst([Status(info["status"]), Status.INCONCLUSIVE]).value
                info["final_state_cross_check"] = "statediff-contradictory"
                reading.problems.append(
                    f"{task_id}: StateDiff records contradictory after-states for this task, so "
                    "the SiloBench final state cannot be cross-checked against a single world"
                )
            elif task_id in statediff_after and isinstance(final, str) and final:
                sd_reading, sd_after = statediff_after[task_id]
                if sd_after != final:
                    info["status"] = worst([Status(info["status"]), Status.INCONCLUSIVE]).value
                    info["final_state_cross_check"] = "mismatch"
                    reading.problems.append(
                        f"{task_id}: SiloBench final state {final} differs from the StateDiff "
                        f"after-state {sd_after} for the same task; absent a shared run id the two "
                        "artifacts cannot be bound to one final world, so this is unverified"
                    )
                    sd_reading.problems.append(
                        f"{task_id}: StateDiff after-state {sd_after} differs from the SiloBench "
                        f"final state {final} for the same task; the two cannot be bound to one run"
                    )
                    sd_reading.status = worst([sd_reading.status, Status.INCONCLUSIVE])
        reading.status = worst([Status(t["status"]) for t in (reading.facts.get("tasks") or {}).values()]
                               or [reading.status])

    goldens = [r for r in readings if r.kind == "environment-golden"]
    if not goldens:
        for reading in readings:
            if reading.kind == "silobench-verify" and reading.facts.get("tasks"):
                reading.problems.append(
                    "no environment-golden evidence collected, so the golden state-hash "
                    "comparison could not be applied"
                )
                if reading.status is Status.PASS:
                    reading.status = Status.INCONCLUSIVE
                    reading.detail += "; golden comparison unavailable"
        return

    golden_tasks, contradictory = _merge_goldens(goldens)
    if contradictory:
        named = ", ".join(sorted(contradictory))
        for reading in goldens:
            # Recorded on every golden, because the disagreement is a property of
            # the set and there is no way to tell which member is wrong. The
            # status drops off NOT_APPLICABLE so the evidence class cannot report
            # reference data as fine while its members contradict each other.
            reading.problems.append(
                f"the collected goldens record different hashes for {named}, so no "
                "committed golden applies to those task(s)"
            )
            reading.status = Status.INCONCLUSIVE

    for reading in readings:
        if reading.kind != "silobench-verify":
            continue
        statuses: list[Status] = []
        for task_id, info in (reading.facts.get("tasks") or {}).items():
            status = Status(info["status"])
            if status is Status.FAIL:
                # Already the strongest verdict. The golden comparison cannot make
                # it worse, and running it would only overwrite the reason it
                # failed.
                statuses.append(status)
                continue
            # A cross-check may already have demoted this task to INCONCLUSIVE. The
            # golden comparison still runs, and the two are combined by worst()
            # below: a golden mismatch is a proven FAIL that must not be masked by
            # the demotion, and a golden match must not lift the demotion to PASS.
            prior = status
            if task_id in contradictory:
                status = Status.INCONCLUSIVE
                info["golden"] = "contradictory (the collected goldens disagree)"
                reading.problems.append(
                    f"{task_id}: the collected goldens disagree about this task, so there "
                    "is no committed hash to compare its final state against"
                )
            elif info.get("is_default_run") is None:
                # Absent is not False. Treating it as False would retire the
                # golden comparison for every task the moment the producer
                # stopped emitting the field, and report PASS while doing it.
                status = Status.INCONCLUSIVE
                info["golden"] = "unknown (producer did not say whether this was a default run)"
                reading.problems.append(
                    f"{task_id}: no is_default_run flag, so it cannot be established "
                    f"whether the committed golden applies to this run"
                )
            elif info["is_default_run"]:
                expected = golden_tasks.get(task_id)
                actual = info.get("final_state_hash")
                if expected is None:
                    status = Status.INCONCLUSIVE
                    info["golden"] = "absent"
                    reading.problems.append(f"{task_id}: no golden hash recorded for this task")
                elif expected != actual:
                    status = Status.FAIL
                    info["golden"] = "mismatch"
                    reading.problems.append(
                        f"{task_id}: checks passed but final state hash {actual} does not "
                        f"match the committed golden {expected}"
                    )
                else:
                    info["golden"] = "match"
            elif non_default_runs_expected:
                # The release declares it runs non-default profiles, so an
                # uncompared final state is a stated condition rather than a check
                # that quietly went missing. But a declared reason for missing
                # evidence is not the evidence: the final world state still was not
                # graded, so this is INCONCLUSIVE (the release can be INCOMPLETE),
                # with the operator's declaration recorded as the reason. A declared
                # claim does not clear a release on its own here, any more than
                # contract_change.declared does; it only makes the gap attributable.
                status = Status.INCONCLUSIVE
                info["golden"] = (
                    "not compared: a non-default run this release declares it expects, so the "
                    "final state is unverified rather than a silent gap"
                )
                reading.problems.append(
                    f"{task_id}: declared non-default run, so the committed golden does not "
                    "apply and the final world state was not graded; the declaration explains "
                    "the gap but does not close it"
                )
            else:
                # SiloBench genuinely does not compare a non-default run against
                # the default golden, so the comparison cannot run. That is a
                # reason it was not performed, not a licence to report it as
                # performed. `is_default_run` is producer-controlled, so leaving
                # PASS here hands the producer a switch that turns off the one
                # check its own `verdict.passed` does not cover.
                status = Status.INCONCLUSIVE
                info["golden"] = "not compared (not a default run)"
                reading.problems.append(
                    f"{task_id}: reported as a non-default run, so the committed golden "
                    "does not apply and this task's final world state was compared against "
                    "nothing; declare environment.expect_non_default_runs in the release "
                    "descriptor if that is intended"
                )
            status = worst([prior, status])
            info["status"] = status.value
            statuses.append(status)
        reading.status = worst(statuses)
        reading.derivations.append("golden state-hash comparison applied")
