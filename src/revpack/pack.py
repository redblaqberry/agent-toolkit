"""Pack lifecycle: collect, decide, seal, verify.

The ordering constraint that shapes this module: a manifest cannot be built
before the artifacts it indexes exist. An earlier design wrote the manifest
during ``collect`` and signed it later, which meant the signature covered a
manifest that predated the decision and the rendered outputs, and stale files
could be sealed unnoticed. Here the manifest is built once, by ``seal``, over
the finished pack.

Two properties do the real work:

- Every derived artifact records ``inputs_digest``, a digest over the hashes of
  its inputs. ``seal`` recomputes it and refuses to sign when it disagrees, so a
  ``decision.json`` left over from an earlier evidence set cannot be sealed.
- ``verify`` recomputes the decision from the sealed evidence and compares. Hash
  checks alone only prove nothing changed after signing; they say nothing about
  whether the decision followed from the evidence at the moment it was signed.
  Recomputation is what closes that.
"""

from __future__ import annotations

import ntpath
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml
from pydantic import ValidationError

from . import attest
from .binding import bind
from .canonical import dump_json, file_sha256, inputs_digest, write_text_atomic
from .errors import InputError, PackError
from .lattice import Status
from . import __version__
from .models import (
    SEMANTICS_VERSION,
    BindingOutcome,
    Decision,
    Envelope,
    EnvelopeFile,
    Manifest,
    ManifestEntry,
    Policy,
    Producer,
    ReleaseDescriptor,
    Rule,
    RuleOutcome,
    TraceabilityMap,
    TraceabilityOutcome,
    Vcs,
)
from .parsers import Reading, cross_reference, detect_format, load_json, parse
from .policy import (
    _rule_slo,
    covered_requirements,
    evaluate_classes,
    evaluate_rules,
    validate_policy,
)

MANIFEST_NAME = "manifest.json"
ENVELOPES_NAME = "envelopes.json"
DECISION_NAME = "decision.json"
POLICY_NAME = "policy.yaml"
RELEASE_NAME = "release.yaml"
TRACE_NAME = "traceability.yaml"

INPUT_NAMES = (POLICY_NAME, RELEASE_NAME, TRACE_NAME)

PRODUCER_BY_KIND = {
    "contract": "discoveryspec",
    "transcript": "discoveryspec",
    "gate-run": "agent-eval-gate",
    "state-verdict": "statediff",
    "blast-radius": "mcp-contract-canary",
    "environment-golden": "silobench",
    "silobench-verify": "silobench",
    "prices": "operator",
}


# ---------------------------------------------------------------------------
# loading typed inputs
# ---------------------------------------------------------------------------


class _StrictYamlLoader(yaml.SafeLoader):
    """A safe loader that refuses a repeated mapping key.

    PyYAML keeps the last value and drops the first, exactly as ``json.loads``
    does, and the three documents this loads are the terms of the decision
    rather than observations of the release. A rule that reads ``blocking:
    true`` to anyone who opens the file and ``blocking: false`` to the engine
    turns a blocking check advisory, and the release then clears carrying the
    evidence that check exists to refuse. The file is copied into the pack,
    hashed, and covered by the signature, so the forgery is archived beside the
    verdict it produced and every later reader sees the version that says
    ``true``.

    Safety is unchanged: this subclasses ``SafeLoader`` and adds a check, so
    ``yaml.load`` below constructs exactly what ``yaml.safe_load`` would.
    """

    def construct_mapping(self, node, deep: bool = False) -> dict:
        seen: list = []
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            # A list rather than a set: an unhashable key is malformed input
            # for these documents, and PyYAML reports it precisely one line
            # later. Refusing it here with a TypeError would be worse.
            if key in seen:
                raise InputError(f"duplicate YAML mapping key {key!r}")
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def _load_yaml_model(path: str | Path, model, label: str):
    try:
        raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_StrictYamlLoader)
    except InputError as exc:
        # Raised by the loader above, which knows the key and not the file.
        raise InputError(f"{label} {path}: {exc}") from exc
    except OSError as exc:
        raise InputError(f"cannot read {label} {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"{label} {path} is not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise InputError(f"{label} {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise InputError(f"{label} {path} must be a mapping")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise InputError(
            f"{label} {path} is not a valid {model.__name__}",
            problems=[f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()],
        ) from exc


def _vcs_for(path: Path) -> Optional[Vcs]:
    """Record the producing tree's git state, or nothing.

    This is an observation about the directory an artifact was read from, not
    proof of who produced it. All it does is run ``git rev-parse HEAD`` and
    ``git status --porcelain`` in that directory: nothing here relates the file
    to the commit, so a hand-written file dropped beside a clean checkout
    records a clean block. Read the result as "this is the tree the file was
    sitting in when it was collected", never as provenance.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        commit = result.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if dirty.returncode != 0:
            # rev-parse answered but status did not, so whether the tree is clean
            # cannot be established. Its return code was ignored, so a failed
            # status left empty stdout and recorded `dirty: false`, a positive
            # clean-tree claim this command never verified. Unknown, not clean:
            # the whole observation is dropped rather than asserted clean.
            return None
        return Vcs(commit=commit, dirty=bool(dirty.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def collect(
    sources: list[tuple[str, str]],
    policy_path: str,
    release_path: str,
    traceability_path: str,
    out: str | Path,
    collected_at: str | None = None,
    record_vcs: bool = True,
) -> Path:
    """Copy evidence and inputs into a pack and write the envelope index."""
    out = Path(out)
    # Every filesystem call in this function is wrapped. A permission error or a
    # full disk is an infrastructure failure and belongs on exit 2; escaping as
    # an OSError it would exit 1, which this project's convention reads as a
    # NO_GO the evidence proves.
    try:
        if out.exists() and any(out.iterdir()):
            raise PackError(
                f"{out} already exists and is not empty; refusing to collect into a pack "
                "that may hold artifacts from an earlier run"
            )
        (out / "evidence").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackError(f"cannot create the pack directory {out}: {exc}") from exc

    stamp = collected_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Validate inputs before copying anything, so a bad policy fails before a
    # half-built pack exists on disk.
    policy = _load_yaml_model(policy_path, Policy, "policy")
    validate_policy(policy)
    _load_yaml_model(release_path, ReleaseDescriptor, "release descriptor")
    _load_yaml_model(traceability_path, TraceabilityMap, "traceability map")

    for src, name in (
        (policy_path, POLICY_NAME),
        (release_path, RELEASE_NAME),
        (traceability_path, TRACE_NAME),
    ):
        try:
            shutil.copyfile(src, out / name)
        except OSError as exc:
            raise PackError(f"cannot copy {src} into the pack: {exc}") from exc

    envelopes: list[Envelope] = []
    for index, (kind, source) in enumerate(sources, start=1):
        source_path = Path(source)
        if not source_path.is_file():
            raise InputError(f"evidence {kind}: {source} is not a file")

        target_dir = out / "evidence" / kind
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PackError(f"cannot create {target_dir}: {exc}") from exc
        target = target_dir / source_path.name
        if target.exists():
            raise InputError(
                f"evidence {kind}: {source_path.name} collides with an artifact already "
                "collected under the same kind; rename one of them"
            )
        # Byte-identical copy. Evidence is never rewritten: altering it would
        # break any producer's own embedded signature and would make the pack's
        # hashes describe something other than what the producer emitted.
        try:
            shutil.copyfile(source_path, target)
        except OSError as exc:
            raise PackError(f"cannot copy evidence {source} into the pack: {exc}") from exc

        try:
            payload = load_json(target) if kind != "transcript" else None
        except InputError:
            payload = None

        envelopes.append(
            Envelope(
                evidence_id=f"EV-{index:04d}",
                kind=kind,
                path=target.relative_to(out).as_posix(),
                sha256=file_sha256(target),
                bytes=target.stat().st_size,
                collected_at=stamp,
                detected_format=detect_format(payload) if payload is not None else "text",
                producer=Producer(name=PRODUCER_BY_KIND.get(kind, "unknown")),
                vcs=_vcs_for(source_path) if record_vcs else None,
            )
        )

    write_text_atomic(
        out / ENVELOPES_NAME, dump_json(EnvelopeFile(envelopes=envelopes).model_dump(mode="json"))
    )
    return out


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def _pack_inputs(pack: Path, envelopes: list[Envelope]) -> list[tuple[str, str]]:
    """The (path, sha256) pairs a decision is a function of."""
    entries = [(ENVELOPES_NAME, file_sha256(pack / ENVELOPES_NAME))]
    for name in INPUT_NAMES:
        entries.append((name, file_sha256(pack / name)))
    for envelope in envelopes:
        entries.append((envelope.path, file_sha256(pack / envelope.path)))
    return entries


def _check_pack_relative(relpath: str, label: str) -> None:
    """Refuse a path that is absolute or escapes the pack.

    Envelope and manifest paths are read from signed documents, but "signed"
    only means somebody vouched for the bytes, not that the bytes are sane. A
    path of ``../outside.json`` would otherwise be read and evaluated from
    outside the pack, where directory closure cannot see it.
    """
    if not relpath or relpath != relpath.strip():
        raise PackError(f"{label}: path {relpath!r} is empty or padded")
    candidate = PurePosixPath(relpath)
    if candidate.is_absolute() or ntpath.isabs(relpath) or ":" in relpath:
        raise PackError(f"{label}: path {relpath!r} must be relative to the pack")
    if any(part in ("..", "") for part in candidate.parts):
        raise PackError(f"{label}: path {relpath!r} must stay inside the pack")


def _resolve_in_pack(pack: Path, relpath: str, label: str) -> Path:
    """Turn a pack-relative path into a real one, proving it stays inside.

    ``_check_pack_relative`` reads the text of the path and nothing else, so it
    cannot see a symbolic link. On its own it would accept
    ``evidence/gate-run/run.json`` while that name pointed at a file anywhere on
    the host, and the pack would then hash and vouch for content it does not
    contain. Comparing resolved paths is the only form of this check that
    survives the filesystem.
    """
    _check_pack_relative(relpath, label)
    root = pack.resolve()
    candidate = (pack / relpath).resolve()
    if candidate != root and root not in candidate.parents:
        raise PackError(
            f"{label}: path {relpath!r} resolves to {candidate}, which is outside the pack"
        )
    return candidate


def read_envelopes(pack: Path) -> list[Envelope]:
    raw = load_json(pack / ENVELOPES_NAME)
    try:
        envelopes = EnvelopeFile.model_validate(raw).envelopes
    except ValidationError as exc:
        raise PackError(f"{ENVELOPES_NAME} is invalid: {exc}") from exc

    seen_ids: set[str] = set()
    # Keyed by resolved path, not by the string the index carries. Those are
    # different questions: `PurePosixPath` drops a `.` segment and collapses a
    # doubled separator before any of the checks above look at the path, so
    # `evidence/x.json` and `evidence/./x.json` are two strings naming one file.
    # Comparing the text would see two artifacts where the filesystem has one,
    # and that single file would count twice toward `min_count` and be read
    # twice by every rule, which is how one piece of evidence satisfies a class
    # that demands several. Resolution also closes the same hole opened through
    # a symbolic link, where the two names need not resemble each other at all.
    seen_paths: dict[Path, str] = {}
    for envelope in envelopes:
        resolved = _resolve_in_pack(pack, envelope.path, f"envelope {envelope.evidence_id}")
        if envelope.evidence_id in seen_ids:
            raise PackError(f"duplicate evidence_id {envelope.evidence_id!r} in {ENVELOPES_NAME}")
        if resolved in seen_paths:
            first = seen_paths[resolved]
            also = "" if first == envelope.path else f" (already indexed as {first!r})"
            raise PackError(
                f"evidence path {envelope.path!r} is indexed more than once in "
                f"{ENVELOPES_NAME}{also}; one artifact would count as several"
            )
        seen_ids.add(envelope.evidence_id)
        seen_paths[resolved] = envelope.path
    return envelopes


def compute_decision(pack: str | Path) -> Decision:
    """Pure function from pack contents to a decision.

    Called by ``decide`` to produce ``decision.json`` and again by ``verify`` to
    check that the stored decision still follows from the sealed evidence. It
    must stay free of wall-clock reads, randomness, and any input outside the
    pack, or verification degrades into comparing a value with itself.
    """
    pack = Path(pack)
    policy = _load_yaml_model(pack / POLICY_NAME, Policy, "policy")
    validate_policy(policy)
    release = _load_yaml_model(pack / RELEASE_NAME, ReleaseDescriptor, "release descriptor")
    trace = _load_yaml_model(pack / TRACE_NAME, TraceabilityMap, "traceability map")

    envelopes = read_envelopes(pack)

    # Envelope hashes and sizes must still describe the files on disk, otherwise
    # the decision would be computed over evidence the index no longer matches.
    # The size is checked as well as the hash: a hash mismatch already catches
    # any change to the bytes, but `bytes` is a field a reader trusts on its own
    # and a stale or hand-edited index can state a count the file never had, so
    # an index that hashes correctly while claiming a false size is refused here
    # rather than sealed as a document whose own numbers disagree with the pack.
    drifted: list[str] = []
    for e in envelopes:
        target = pack / e.path
        if not target.is_file():
            drifted.append(f"{e.path}: missing or modified since collection")
            continue
        if file_sha256(target) != e.sha256:
            drifted.append(f"{e.path}: missing or modified since collection")
        elif target.stat().st_size != e.bytes:
            drifted.append(
                f"{e.path}: envelope records {e.bytes} bytes but the file is "
                f"{target.stat().st_size}"
            )
    if drifted:
        raise PackError(
            "evidence on disk does not match the envelope index",
            problems=drifted,
        )

    readings: list[Reading] = [
        parse(e.evidence_id, e.kind, pack / e.path, e.sha256) for e in envelopes
    ]
    # The release descriptor is the only place an operator can state that a
    # non-default SiloBench profile is intended, so the cross-reference is told
    # what was declared rather than deducing it from the evidence it is checking.
    cross_reference(readings, release.environment.expect_non_default_runs)

    bindings = bind(release, readings)

    # The traceability map is an input rather than collected evidence, so its
    # binding check lives here instead of in bind(). A join authored against a
    # different contract carries requirement ids whose meaning may have shifted
    # underneath it, which is exactly how a stale map launders a bad provenance
    # claim into a signed dossier.
    trace_bound = trace.contract_sha256 == release.contract_sha256
    bindings.append(
        BindingOutcome(
            evidence_id="traceability-map",
            kind="traceability-map",
            check="contract_sha256",
            status=Status.PASS if trace_bound else Status.FAIL,
            detail=(
                f"map was authored against the released contract {trace.contract_sha256[:12]}"
                if trace_bound
                else f"map was authored against contract {trace.contract_sha256[:12]} but the "
                f"release declares {release.contract_sha256[:12]}; the map is stale"
            ),
        )
    )

    # The transcript is tied to the release by its bytes in bind(), but the
    # CONTRACT records the transcript it was compiled from, and nothing checked
    # that the release's declared transcript is the contract's. A release could
    # declare transcript X, collect bytes hashing to X, and sit beside a contract
    # compiled from Y, with both the contract and transcript bindings passing while
    # the transcript in the pack is not the one the approved contract was built on.
    # This cross-check closes that: the contract's recorded transcript hash must
    # equal the release's declared one for the chain to hold.
    _contract = next((r for r in readings if r.kind == "contract"), None)
    _contract_transcript = _contract.facts.get("transcript_sha256") if _contract else None
    if release.transcript_sha256 and _contract is not None:
        if _contract_transcript is None:
            # The release declares a transcript and an approved contract is
            # present, but the contract records no transcript it was compiled
            # from, so the transcript-to-contract link cannot be confirmed.
            # Skipping the check because the field was absent was the hole: a
            # contract with its transcript hash stripped bound cleanly, and the
            # pack could go GO without evidence that the approved contract was
            # built from the transcript in the pack. Unverifiable, so INCONCLUSIVE.
            bindings.append(
                BindingOutcome(
                    evidence_id=_contract.evidence_id,
                    kind="contract",
                    check="contract_transcript_sha256",
                    status=Status.INCONCLUSIVE,
                    detail=(
                        "the release declares a transcript but the approved contract records no "
                        "transcript it was compiled from, so the chain from transcript to contract "
                        "cannot be confirmed"
                    ),
                )
            )
        else:
            _tied = _contract_transcript == release.transcript_sha256
            bindings.append(
                BindingOutcome(
                    evidence_id=_contract.evidence_id,
                    kind="contract",
                    check="contract_transcript_sha256",
                    status=Status.PASS if _tied else Status.FAIL,
                    detail=(
                        f"contract was compiled from transcript {str(_contract_transcript)[:12]} "
                        f"and the release declares {release.transcript_sha256[:12]}"
                        + ("" if _tied else "; the transcript is not the one the contract used")
                    ),
                )
            )

    notes: list[str] = [] if trace_bound else [bindings[-1].detail]

    classes = evaluate_classes(policy, readings, trace)
    rules = evaluate_rules(policy, readings, trace, release, bindings)

    contract = next((r for r in readings if r.kind == "contract"), None)
    # Floor: a declared contract SLO is enforced whether or not the policy carries
    # a blocking slo rule. Rule selection is the operator's, but switching off an
    # approved contract TERM is not: a policy that omits the slo rule (or makes it
    # advisory) left the ceiling the contract states unenforced, so a run breaching
    # it cleared because nobody wrote the rule. When the contract declares an slo
    # and no blocking slo rule already covers it, the ceiling is checked here as a
    # blocking rule the policy cannot remove.
    if (
        contract is not None
        and (contract.facts.get("slo") or {})
        and not any(r.kind == "slo" and r.blocking for r in rules)
    ):
        rules.append(
            _rule_slo(
                Rule(id="R-FLOOR-SLO", kind="slo", blocking=True),
                readings=readings,
                release=release,
            )
        )
    # Floor: a release that declares an MCP contract change must carry the canary
    # blast-radius analysis of that change. Declaring the change is a statement
    # that its consumer impact was assessed; a policy whose required_evidence omits
    # blast-radius would otherwise let a declared change clear with no analysis at
    # all, which is the exact gap this tool exists to close. Absent the report the
    # release is INCOMPLETE, via a blocking rule the policy cannot switch off.
    if release.contract_change.declared and not any(
        r.kind == "blast-radius" for r in readings
    ):
        rules.append(
            RuleOutcome(
                id="R-FLOOR-BLAST-RADIUS",
                kind="blast_radius_required",
                blocking=True,
                status=Status.INCONCLUSIVE,
                detail=(
                    "the release declares an MCP contract change but the pack carries no canary "
                    "blast-radius report, so the consumer impact of the declared change was "
                    "never assessed"
                ),
            )
        )
    declared_reqs = set((contract.facts.get("requirement_ids") if contract else []) or [])
    # Resolved against collected evidence, by the same function the blocking
    # coverage rule uses. Counting every requirement a map entry merely mentions
    # let this summary call a requirement covered in the same document where that
    # rule reported it has no executing evidence at all.
    resolved_reqs = covered_requirements(readings, trace)
    # A requirement the map names but the contract does not contain is dangling
    # whether or not it resolves to evidence, so this one stays a plain reading
    # of the map.
    mapped_reqs = {e.requirement for e in trace.entries}
    conflicts = [f for r in rules if r.kind == "traceability_conflict" for f in r.findings]

    fail_count = sum(
        1 for item in [*classes, *rules] if item.status is Status.FAIL
    ) + sum(1 for b in bindings if b.status is Status.FAIL)
    inconclusive_count = sum(
        1 for item in [*classes, *rules] if item.status is Status.INCONCLUSIVE
    ) + sum(1 for b in bindings if b.status is Status.INCONCLUSIVE)

    state = _decide_state(classes, rules, readings)

    return Decision(
        revpack_version=__version__,
        semantics_version=SEMANTICS_VERSION,
        state=state,
        policy_id=policy.id,
        release_id=release.id,
        inputs_digest=inputs_digest(_pack_inputs(pack, envelopes)),
        evidence_classes=classes,
        rules=rules,
        bindings=bindings,
        traceability=TraceabilityOutcome(
            declared_entries=len(trace.entries),
            covered_requirements=sorted(declared_reqs & resolved_reqs),
            uncovered_requirements=sorted(declared_reqs - resolved_reqs),
            conflicts=conflicts,
            dangling=sorted(mapped_reqs - declared_reqs),
        ),
        fail_count=fail_count,
        inconclusive_count=inconclusive_count,
        derivations=sorted({d for r in readings for d in r.derivations}),
        notes=notes + sorted({p for r in readings for p in r.problems}),
    )


def _decide_state(classes: list, rules: list, readings: list) -> str:
    """GO, NO_GO, or INCOMPLETE, in that precedence order.

    NO_GO dominates INCOMPLETE: a proven violation is a decision that can be
    defended, and reporting "we could not tell" over a known failure would
    understate it. Both counts appear in the decision either way, so the
    precedence hides nothing.

    Only FAIL and INCONCLUSIVE used to be read, which made NOT_APPLICABLE on a
    blocking rule a silent pass. That is the general hole: any rule that returns
    "does not apply", whether by design, by a gap in an implementation, or
    because someone stubbed it out, clears a release the policy declared it must
    not clear. Enumerating the statuses that block is safer than enumerating the
    ones that do not, so this asks for PASS explicitly and treats everything
    else as unresolved.

    The decision is also read against the RAW readings, not only the classes the
    policy declared. A policy requiring only contract and gate-run left a
    collected, correctly bound state-verdict that normalized to FAIL out of every
    class and every rule, so a proven failure sitting in the pack disappeared and
    the release cleared. Collected evidence of a failure blocks whether or not the
    policy asked for it: dropping evidence you hold because you did not require it
    is the exact laundering this tool exists to prevent.
    """
    blocking_rules = [r for r in rules if r.blocking]
    if (
        any(r.status is Status.FAIL for r in blocking_rules)
        or any(c.status is Status.FAIL for c in classes)
        or any(r.status is Status.FAIL for r in readings)
    ):
        return "NO_GO"
    # NOT_APPLICABLE is legitimate on an evidence class (reference data asserts
    # nothing about a release and is not pretending to) and on an advisory rule.
    # On a rule the policy marked blocking it is a gap: the policy said this
    # release must not clear without this check, and the check did not run.
    # A collected reading that could not be established (INCONCLUSIVE) blocks to
    # INCOMPLETE for the same reason a FAIL blocks to NO_GO: it is evidence held in
    # the pack that the policy's classes may not cover. Under an `authoritative`
    # reduce, the demoted run is exactly this: parsed INCONCLUSIVE (a trajectory
    # error, say) yet not the class's decider, so its own status would otherwise
    # never be read. NOT_APPLICABLE readings (reference data) do not block.
    if (
        any(r.status is not Status.PASS for r in blocking_rules)
        or any(c.status is Status.INCONCLUSIVE for c in classes)
        or any(r.status is Status.INCONCLUSIVE for r in readings)
    ):
        return "INCOMPLETE"
    return "GO"


def decide(pack: str | Path) -> Decision:
    pack = Path(pack)
    decision = compute_decision(pack)
    write_text_atomic(pack / DECISION_NAME, dump_json(decision.model_dump(mode="json")))
    return decision


# ---------------------------------------------------------------------------
# seal
# ---------------------------------------------------------------------------


def _load_decision(path: Path) -> Decision:
    """Read decision.json, converting a schema failure into a clean PackError.

    A raw pydantic ValidationError escaping here would surface as a traceback
    during seal or verify, which is exactly when a caller most needs a
    deterministic exit code.
    """
    try:
        return Decision.model_validate(load_json(path))
    except ValidationError as exc:
        raise PackError(
            f"{path.name} is not a valid decision.v1 document",
            problems=[f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()],
        ) from exc


def _categorize(relpath: str) -> str:
    if relpath.startswith("evidence/"):
        return "evidence"
    if relpath in INPUT_NAMES:
        return "input"
    if relpath == ENVELOPES_NAME:
        return "index"
    return "derived"


def _pack_files(pack: Path) -> list[str]:
    """Every file in the pack except the manifest, as sorted POSIX relpaths.

    Walked explicitly rather than with ``rglob``, and symbolic links are refused
    rather than skipped. The two behaviours ``rglob`` combines are both wrong
    here and they are wrong in opposite directions:

    - It does not descend into a directory symlink, so a whole subtree could sit
      inside a pack while closure never saw it. Every file in the pack would
      have a manifest entry, every entry would have a file, and verification
      would report the pack complete while unmanifested content travelled with
      it.
    - ``is_file()`` does follow a file symlink, so a link would be hashed and
      indexed as though the bytes lived in the pack. They do not: the target can
      be replaced afterwards without touching a byte the manifest covers, and a
      pack copied elsewhere loses them entirely.

    A pack is a self-contained set of bytes or it is not a pack, so a link is
    refused outright rather than quietly resolved or quietly ignored.
    """
    files: list[str] = []
    directories = [pack]
    while directories:
        current = directories.pop()
        try:
            children = sorted(current.iterdir())
        except OSError as exc:
            raise PackError(f"cannot read pack directory {current}: {exc}") from exc
        for child in children:
            # as_posix(), not str().replace("\\", "/"): on POSIX a backslash is a
            # legal filename byte, so the blunt replace aliased a file literally
            # named "a\b.json" onto the path "a/b.json". The manifest then indexed
            # a name that resolved to a different file (or to nothing), and its
            # content check was skipped while closure still balanced. as_posix()
            # only rewrites the separator the running platform actually uses.
            rel = child.relative_to(pack).as_posix()
            if child.is_symlink():
                raise PackError(
                    f"{rel}: symbolic link inside the pack; a pack must hold real files, "
                    "because a link's target can be replaced without changing any byte "
                    "the manifest covers"
                )
            if child.is_dir():
                directories.append(child)
                continue
            if not child.is_file():
                # A socket, device node, or FIFO. Not content, not hashable, and
                # not something a manifest can honestly describe.
                raise PackError(
                    f"{rel}: not a regular file; a pack indexes file content and cannot "
                    "describe anything else"
                )
            if rel == MANIFEST_NAME:
                continue
            files.append(rel)
    return sorted(files)


def seal(pack: str | Path, key_path: str, pack_id: str | None = None) -> Manifest:
    """Build the manifest over the finished pack and sign it."""
    pack = Path(pack)
    # The signing key must live OUTSIDE the pack. `_pack_files` indexes every
    # regular file it finds, so a private key sitting inside the pack directory
    # would be hashed, manifested, signed, and shipped with the dossier, handing
    # the private signing authority to anyone the pack is published to. Refuse
    # before a single byte is read.
    try:
        key_resolved = Path(key_path).resolve()
        pack_resolved = pack.resolve()
    except OSError as exc:
        raise PackError(f"cannot resolve signing key or pack path: {exc}") from exc
    if key_resolved == pack_resolved or pack_resolved in key_resolved.parents:
        raise PackError(
            f"the signing key {key_path} is inside the pack {pack}; sealing would index and "
            "publish the private key. Keep the key outside the pack directory."
        )
    decision_path = pack / DECISION_NAME
    if not decision_path.is_file():
        raise PackError(
            f"{DECISION_NAME} is missing; run `revpack decide` before sealing, because a "
            "manifest must index the decision it vouches for"
        )

    stored = _load_decision(decision_path)

    # Refuse to sign a decision this build cannot vouch for. The manifest below
    # is stamped with the semantics version of the build that signs, and
    # `verify` refuses any pack whose decision declares a different one, so
    # decide, upgrade across a semantics change, then seal produces a correctly
    # signed pack that verification rejects forever and reports as an edited
    # decision. Naming it here costs one comparison; discovering it at
    # verification time costs whatever the pack was archived to prove.
    if stored.semantics_version != SEMANTICS_VERSION:
        raise PackError(
            "refusing to sign a decision computed under rule semantics this build does not "
            "implement",
            problems=[
                f"{DECISION_NAME} declares semantics v{stored.semantics_version} and this "
                f"build implements v{SEMANTICS_VERSION}; rerun `revpack decide` so the "
                "signature covers a decision these rules produced"
            ],
        )

    # Refuse to seal a decision that does not describe the evidence beside it.
    # This is what stops a pack built from one evidence set and a decision left
    # over from another from being signed as a matching pair.
    envelopes = read_envelopes(pack)
    expected = inputs_digest(_pack_inputs(pack, envelopes))
    if stored.inputs_digest != expected:
        raise PackError(
            "decision.json was produced from different inputs than the pack now contains",
            problems=[
                f"decision records inputs_digest {stored.inputs_digest[:12]}, "
                f"the pack hashes to {expected[:12]}; rerun `revpack decide`"
            ],
        )

    # Every file the manifest signs must be one the decision was a function of.
    # The decision is computed over the enveloped files only (`_pack_inputs`),
    # while the manifest below signs every file in the pack. A file dropped into
    # the pack with no envelope is therefore sealed and categorized as evidence
    # yet never read by decide or verify: a failing artifact could ride inside a
    # GO pack, signed and ignored, and verify would still recompute GO. Refuse to
    # seal until the files present and the files described agree.
    pack_files = _pack_files(pack)
    known = set(INPUT_NAMES) | {ENVELOPES_NAME, DECISION_NAME} | {e.path for e in envelopes}
    undescribed = sorted(f for f in pack_files if f not in known)
    if undescribed:
        raise PackError(
            "the pack holds files no envelope describes, so the decision does not cover them",
            problems=[
                f"{f}: present in the pack and about to be signed as "
                f"{_categorize(f)!r}, but described by no envelope, so decide and verify "
                "never read it; rebuild the pack so every file is collected through an envelope"
                for f in undescribed
            ],
        )

    entries = [
        ManifestEntry(
            path=rel,
            sha256=file_sha256(pack / rel),
            bytes=(pack / rel).stat().st_size,
            category=_categorize(rel),
        )
        for rel in pack_files
    ]

    manifest = Manifest(
        pack_id=pack_id or stored.release_id,
        revpack_version=__version__,
        semantics_version=SEMANTICS_VERSION,
        entries=entries,
    )
    private = attest.load_private_key(key_path)
    signed = attest.sign(manifest.model_dump(mode="json", exclude_none=True), private)
    write_text_atomic(pack / MANIFEST_NAME, dump_json(signed))
    return Manifest.model_validate(signed)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

# The manifest is the one document this tool parses before it can check who
# wrote it, so its ingestion is bounded. Neither ceiling can be reached by an
# honest pack: a release dossier with more than a hundred thousand files is not
# a release dossier, and that many entries encode to a few tens of megabytes.
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 100_000


def _read_manifest(manifest_path: Path) -> tuple[Manifest, dict]:
    """Parse the manifest under a size and entry-count bound.

    Returns the model and the raw document, because the attestation is verified
    over the raw form: re-serializing a validated model could change the bytes
    the signature was taken over.
    """
    try:
        size = manifest_path.stat().st_size
    except OSError as exc:
        raise PackError(f"cannot read {MANIFEST_NAME}: {exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise PackError(
            f"{MANIFEST_NAME} is {size} bytes, above the {MAX_MANIFEST_BYTES} byte ceiling; "
            "refusing to parse an unauthenticated document of unbounded size"
        )

    raw = load_json(manifest_path)
    if not isinstance(raw, dict):
        raise PackError(f"{MANIFEST_NAME} must be a JSON object")
    entries = raw.get("entries")
    if isinstance(entries, list) and len(entries) > MAX_MANIFEST_ENTRIES:
        # Counted before validation. Pydantic would otherwise build a model
        # object per entry first, which is the cost this bound exists to avoid.
        raise PackError(
            f"{MANIFEST_NAME} lists {len(entries)} entries, above the "
            f"{MAX_MANIFEST_ENTRIES} entry ceiling"
        )
    try:
        return Manifest.model_validate(raw), raw
    except ValidationError as exc:
        raise PackError(f"{MANIFEST_NAME} is invalid: {exc}") from exc


@dataclass(frozen=True)
class VerifyResult:
    """What verification established, and what it could not.

    Two outcomes rather than one. A pack whose decision could not be recomputed
    has had its integrity confirmed and its verdict confirmed by nobody, and
    those are not the same result. ``unrecheckable`` is a structured field
    rather than something a caller greps out of ``notes``, because the CLI's
    exit code depends on it and an exit code that depends on the wording of a
    message breaks the moment somebody improves the wording.

    ``state`` is here for the same reason. Verifying a pack establishes which
    verdict the evidence yields, and a caller writing ``revpack verify && ship``
    needs that verdict, not merely the news that the bytes are intact. Returning
    it as a field keeps the CLI from having to re-read ``decision.json`` and
    trust a copy nothing in this function vouched for.
    """

    notes: list[str]
    # The verdict this run confirmed follows from the sealed evidence, or None
    # when the decision could not be rechecked. It is deliberately NOT a copy of
    # the stored state: it is only set after recomputation has agreed with it, so
    # reading it can never report a forged verdict as an established one.
    state: Optional[str] = None
    # None when the decision was recomputed and matched. Otherwise the reason it
    # could not be, which is a gap in this run rather than a fault in the pack.
    unrecheckable: Optional[str] = None

    def __post_init__(self) -> None:
        # Exactly one of the two must be set. A result carrying neither would
        # claim a verdict it never established, and a result carrying both would
        # leave the caller to pick which half to believe. This is checked rather
        # than assumed because the CLI's exit code is derived from it, and a
        # construction site that forgot to name the state would otherwise fall
        # through to a silent success.
        if (self.state is None) == (self.unrecheckable is None):
            raise ValueError(
                "a verify result must name either the decision state it confirmed or the "
                "reason it could not confirm one, never both and never neither"
            )

    @property
    def decision_rechecked(self) -> bool:
        return self.unrecheckable is None


def verify(pack: str | Path, public_key_path: str) -> VerifyResult:
    """Full verification. Returns a ``VerifyResult``; raises on any failure.

    Order matters, and the order is:

    1. Closure, over an unauthenticated manifest. Cheap set arithmetic, and the
       only check that can name a fault the signature cannot: adding or deleting
       a file after sealing leaves the signature perfectly valid.
    2. The attestation over the manifest body.
    3. Content, size, and category, per file. This runs after the signature
       because it is the expensive part and because a manifest edit is better
       diagnosed as "modified after signing" than as a content mismatch.
    4. Recomputation of the decision from the sealed evidence.
    """
    pack = Path(pack)
    manifest_path = pack / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PackError(f"{MANIFEST_NAME} is missing; the pack is not sealed")

    manifest, raw = _read_manifest(manifest_path)

    problems: list[str] = []

    # 1. Closure. Every file except the manifest has exactly one entry, and
    #    every entry has a file. The manifest excludes itself by construction:
    #    it cannot contain a hash of bytes that include that hash, and adding
    #    the signature would change them again. Its integrity comes from step 2.
    #
    #    This runs before the signature because it is cheap (set arithmetic over
    #    the entry list, no file reads) and because it is the one check the
    #    signature cannot stand in for: a file added to or deleted from a sealed
    #    pack leaves the manifest bytes, and therefore the signature, perfectly
    #    valid. Naming that fault needs closure, not cryptography.
    on_disk = set(_pack_files(pack))
    indexed = [entry.path for entry in manifest.entries]
    # Counter rather than list.count() per entry: the latter is quadratic, and
    # this runs over an attacker-supplied manifest before the signature has been
    # checked.
    duplicates = sorted(path for path, count in Counter(indexed).items() if count > 1)
    indexed_set = set(indexed)

    # The lexical half of the path check only. Resolving a path touches the
    # filesystem, so that half waits until step 3, where the manifest has been
    # authenticated and the work is bounded.
    for entry in manifest.entries:
        _check_pack_relative(entry.path, "manifest entry")

    problems += [f"{path}: present in the pack but absent from the manifest" for path in sorted(on_disk - indexed_set)]
    problems += [f"{path}: listed in the manifest but absent from the pack" for path in sorted(indexed_set - on_disk)]
    problems += [f"{path}: listed more than once in the manifest" for path in duplicates]

    if problems:
        # Raised here rather than pooled with the content findings below. Once
        # closure holds, the entry list and the files on disk are the same set
        # with no repeats, so the per-file hashing that follows costs exactly
        # one pass over the pack's real bytes. Pooling the two would mean
        # hashing whatever an unauthenticated manifest asked for, and a manifest
        # naming one large file ten thousand times would buy ten thousand hashes
        # before anyone checked who wrote it.
        raise PackError("pack failed structural verification", problems=problems)

    # 2. Attestation over the manifest body.
    public = attest.load_public_key(public_key_path)
    attest.verify(raw, public)

    # 3. Content, plus the recorded size and category. A signed manifest can
    #    still carry a false size or category: the signature says who wrote the
    #    numbers, not that the numbers are true. Leaving them unchecked would
    #    make them decorative fields that a reader might nonetheless trust.
    for entry in manifest.entries:
        target = _resolve_in_pack(pack, entry.path, "manifest entry")
        if not target.is_file():
            continue
        actual = file_sha256(target)
        if actual != entry.sha256:
            problems.append(
                f"{entry.path}: content hash {actual[:12]} does not match the manifest "
                f"entry {entry.sha256[:12]}"
            )
        actual_bytes = target.stat().st_size
        if actual_bytes != entry.bytes:
            problems.append(
                f"{entry.path}: size {actual_bytes} does not match the manifest entry "
                f"{entry.bytes}"
            )
        expected_category = _categorize(entry.path)
        if expected_category != entry.category:
            problems.append(
                f"{entry.path}: category {entry.category!r} does not match its location "
                f"(expected {expected_category!r})"
            )

    if problems:
        raise PackError("pack failed structural verification", problems=problems)

    # 4. Chain and recomputation. Hash checks prove nothing changed after
    #    signing. They cannot tell whether the decision followed from the
    #    evidence at the moment it was signed, so recompute it.
    stored = _load_decision(pack / DECISION_NAME)
    notes = [f"pack {manifest.pack_id}: {len(manifest.entries)} file(s) verified"]

    # The decision's own semantics_version is not evidence about anything: it
    # sits inside decision.json, which is the file a forger edits. The manifest
    # carries the same number, stamped by the build that actually sealed the
    # pack, and step 2 has just proved that copy is the one the signer vouched
    # for. Comparing the two before either is used is what stops "declare a
    # semantics version this build does not implement" from being a switch that
    # turns recomputation off: forging a verdict and bumping the version used to
    # verify clean, because the skip below was reached before anything checked
    # the version against a trustworthy source.
    if stored.semantics_version != manifest.semantics_version:
        raise PackError(
            "the sealed decision claims rule semantics the sealing build did not implement",
            problems=[
                f"decision.json declares semantics v{stored.semantics_version} but the "
                f"signed manifest records v{manifest.semantics_version}; one build wrote "
                "both, so they cannot disagree unless the decision was edited"
            ],
        )

    if manifest.semantics_version != SEMANTICS_VERSION:
        # Recomputing under different rule semantics and reporting the
        # difference as tampering would be a false accusation. Integrity is
        # fully established by steps 1 to 3; what cannot be re-established is
        # the verdict, and saying so plainly is the honest outcome. It is still
        # not a pass, which is why this returns a result the CLI exits 2 on
        # rather than a note buried in a green report.
        return VerifyResult(
            notes=notes,
            state=None,
            unrecheckable=(
                f"integrity verified, but the decision was NOT rechecked: the pack was "
                f"sealed under semantics v{manifest.semantics_version} by revpack "
                f"{stored.revpack_version} and this build implements v{SEMANTICS_VERSION}"
            ),
        )

    recomputed = compute_decision(pack)
    if recomputed.semantic_core() != stored.semantic_core():
        raise PackError(
            "the sealed decision does not follow from the sealed evidence",
            problems=_diff_decisions(stored, recomputed),
        )

    notes.append(f"decision {stored.state} recomputed and matches")
    if recomputed.model_dump(mode="json") != stored.model_dump(mode="json"):
        # Wording drifted between the build that sealed this pack and this one.
        # Every status and the verdict itself match, so this is informational,
        # not a failure.
        notes.append(
            f"explanatory text differs from this build's wording (sealed by revpack "
            f"{stored.revpack_version}, this is {__version__}); the verdict and every "
            "status match"
        )
    # The recomputed state, not the stored one. They are equal here by the check
    # above, and taking the recomputed copy means the value the caller acts on
    # was derived from the evidence in this process rather than read out of the
    # document a forger would edit.
    return VerifyResult(notes=notes, state=recomputed.state)


def _diff_decisions(stored: Decision, recomputed: Decision) -> list[str]:
    """Explain a semantic mismatch. Prose differences are deliberately absent:
    they are not part of the comparison and reporting them would bury the real
    finding under cosmetic noise."""
    differences = []
    if stored.state != recomputed.state:
        differences.append(
            f"state: sealed as {stored.state}, the evidence yields {recomputed.state}"
        )
    if stored.inputs_digest != recomputed.inputs_digest:
        differences.append(
            f"inputs_digest: sealed {stored.inputs_digest[:12]}, recomputed "
            f"{recomputed.inputs_digest[:12]}"
        )
    stored_rules = {r.id: r.status for r in stored.rules}
    for rule in recomputed.rules:
        sealed = stored_rules.get(rule.id)
        if sealed != rule.status:
            sealed_label = sealed.value if sealed is not None else "absent"
            differences.append(
                f"rule {rule.id}: sealed as {sealed_label}, recomputed as {rule.status.value}"
            )
    if not differences:
        differences.append("decision differs from a recomputation in a field-level detail")
    return differences
