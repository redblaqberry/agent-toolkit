"""discoveryspec CLI.

Current surface: ``validate``, ``compile``, ``approve``, ``export-gate``,
``run``, and ``report``, the full pipeline from transcript to provenance-linked
gate verdicts and a brand-governed manager report, plus ``keygen`` for the
Ed25519 approval key the signed workflow runs on.

Exit codes (fail closed, same convention as agent-eval-gate):
  0  structurally valid; draft findings (conflicts, open questions, pending
     fields) are reported, they are the product working as intended
  1  quality failure: the contract claims approved but has open conflicts,
     blocking open questions, missing sign-off, or unfilled required fields;
     approve also exits 1 for a draft that is not fully resolved, export-gate
     for any contract that is not approved and clean, and run for failing
     scenarios or breached SLOs
  2  infrastructure/structural failure: unreadable file, schema or model
     violation, broken provenance, dangling references, malformed transcript,
     refused extraction, harness or judge errors during a run
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import NoReturn, Optional

import typer
import yaml

from .approve import ApprovalError, approve_contract
from .attest import (
    AttestationError,
    contract_has_signature,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_fingerprint,
    run_has_signature,
    sign_contract,
    sign_run,
    verify_contract,
    verify_run,
)
from .brand import DEFAULT_BRAND, BrandError, load_brand
from .report import ReportError, render_report
from .extraction import (
    DEFAULT_EXTRACTION_MODEL,
    AnthropicExtractor,
    ExtractionError,
    Extractor,
    StubExtractor,
    compile_contract,
)
from .loader import (
    ContractLoadError,
    contract_from_raw,
    load_contract,
    load_contract_raw,
    parse_contract_json,
)
from .models import DeploymentContract
from .runner import (
    RunError,
    console_lines,
    evaluate_slos,
    execute_scenarios,
    provenance_report,
    require_gate,
    run_exit_code,
    scenario_cost_eur,
    validate_price_table,
)
from .scenarios import CompileError, gate_export
from .transcript import Transcript, parse_transcript
from .validate import ValidationReport, validate_contract

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """Compile discovery transcripts into reviewed, executable deployment contracts."""


def _echo_load_error_and_exit(exc: ContractLoadError) -> NoReturn:
    typer.echo(f"INVALID: {exc.path}", err=True)
    for problem in exc.problems:
        typer.echo(f"  - {problem}", err=True)
    raise typer.Exit(code=2)


def _refuse_existing_out(out: Path) -> NoReturn:
    typer.echo(
        f"error: {out} already exists; an existing sign-off artifact is never "
        f"replaced silently. Pass --force to overwrite it.",
        err=True,
    )
    raise typer.Exit(code=2)


def _refuse_out_aliasing(out: Path, source: Path, label: str) -> None:
    """Refuse an --out that is the same file as a command input.

    Compared by resolved path AND file identity: a hard link to the input has
    a different name but the same file, and writing it would still destroy
    the input. --force never overrides this.
    """
    if out.resolve() == source.resolve() or (
        out.exists() and source.exists() and out.samefile(source)
    ):
        typer.echo(
            f"error: --out {out} is the {label} this command just read; "
            f"inputs are never overwritten (--force does not override this).",
            err=True,
        )
        raise typer.Exit(code=2)


def _echo_any_console(text: str, err: bool = False) -> None:
    """Echo text that may carry characters outside the console's codepage.

    Check details and quoted statements can contain characters (agent-eval-gate
    uses a set-inclusion symbol) that a Windows cp125x console cannot encode;
    a run summary must degrade to replacement characters, never crash.
    """
    try:
        typer.echo(text, err=err)
    except UnicodeEncodeError:
        stream = sys.stderr if err else sys.stdout
        encoding = getattr(stream, "encoding", None) or "utf-8"
        typer.echo(
            text.encode(encoding, errors="replace").decode(encoding), err=err
        )


def _write_all_atomic(out: Path, files: dict[str, str]) -> None:
    """Write a whole set of artifacts transactionally: stage every file as a
    temp sibling, move any existing finals aside as backups, then commit all
    replacements; on ANY failure the backups are restored, so the visible set
    is either the complete new set or the complete old set, never a mix.

    Guarantees, stated honestly: os.replace is atomic per file, not per set,
    so a reader racing the commit loop can observe a transient partial state;
    the output directory is single-writer by contract (the .tmp/.bak sibling
    names are fixed, concurrent invocations against one --out are
    unsupported). If the rollback itself fails, that is REPORTED with the
    leftover paths, never swallowed, so a partial set cannot pose as clean."""
    import os

    temps: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        out.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            final = out / name
            temp = out / (name + ".tmp")
            # newline="\n" keeps the on-disk bytes identical to the content
            # string on every platform; run-report.json pins run.json by the
            # sha256 of exactly these bytes
            temp.write_text(content, encoding="utf-8", newline="\n")
            temps.append((temp, final))
        for temp, final in temps:
            if final.exists():
                backup = out / (final.name + ".bak")
                os.replace(final, backup)
                backups.append((backup, final))
        for temp, final in temps:
            os.replace(temp, final)
            committed.append(final)
    except OSError as exc:
        leftovers: list[str] = []
        for path in committed:
            try:
                path.unlink()
            except OSError:
                leftovers.append(str(path))
        for backup, final in backups:
            try:
                os.replace(backup, final)
            except OSError:
                leftovers.append(str(backup))
        for temp, _ in temps:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                leftovers.append(str(temp))
        typer.echo(f"error: cannot write artifacts under {out}: {exc}", err=True)
        if leftovers:
            typer.echo(
                "error: rollback was incomplete; inspect these paths before "
                "trusting the output directory:",
                err=True,
            )
            for path in leftovers:
                typer.echo(f"  - {path}", err=True)
        raise typer.Exit(code=2)
    for backup, _ in backups:
        try:
            backup.unlink()
        except OSError:
            pass


def _write_gate_artifacts(out: Path, scenarios_payload: dict, config_payload: dict) -> None:
    _write_all_atomic(out, {
        "scenarios.yaml": yaml.safe_dump(
            scenarios_payload, sort_keys=False, allow_unicode=True, width=100
        ),
        "gate-config.json": json.dumps(
            config_payload, indent=2, ensure_ascii=False
        ) + "\n",
    })


def _remove_stale_artifacts(*paths: Path, quiet: bool = False) -> None:
    # a refused export or run must not leave previously produced artifacts behind
    for stale in paths:
        if stale.exists():
            stale.unlink()
            if not quiet:
                typer.echo(f"removed stale artifact {stale}", err=True)


def _write_contract_or_exit(out: Path, document: dict, force: bool) -> None:
    # exclusive creation, not check-then-write: two concurrent writers racing
    # for the same path must not silently truncate each other's artifact
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    except FileExistsError:
        _refuse_existing_out(out)
    except OSError as exc:
        typer.echo(f"error: cannot write {out}: {exc}", err=True)
        raise typer.Exit(code=2)


def _run_provenance(gate, price_table_sha256: str, price_table_name: str) -> dict:
    """Version and input identity recorded in every run report, so a report
    can be traced to the code and price data that produced it. The Git commit
    is deliberately omitted here (the library does not read the repo); it is
    the operator's responsibility to run from a committed tree for release
    evidence."""
    import platform

    from . import __version__ as ds_version

    return {
        "discoveryspec_version": ds_version,
        "agent_eval_gate_version": getattr(gate, "__version__", "unknown"),
        "python_version": platform.python_version(),
        "price_table_name": price_table_name,
        "price_table_sha256": price_table_sha256,
    }


def _load_contract_or_exit(contract_path: Path) -> DeploymentContract:
    try:
        return load_contract(contract_path)
    except ContractLoadError as exc:
        _echo_load_error_and_exit(exc)


def _load_contract_snapshot_or_exit(
    contract_path: Path,
) -> tuple[DeploymentContract, str, dict]:
    """Load a contract, hash THE SAME BYTES the model was parsed from, and
    return the raw dict too (for signature verification against the exact
    bytes executed).

    Reading the file twice would let a concurrent replacement split the pair:
    the executed contract and the recorded hash must never describe different
    bytes, or the run/report binding becomes a claim about a file that was
    never executed."""
    try:
        raw_bytes = contract_path.read_bytes()
        raw = parse_contract_json(raw_bytes.decode("utf-8"))
        loaded = contract_from_raw(raw, str(contract_path))
    except ContractLoadError as exc:
        _echo_load_error_and_exit(exc)
    except (OSError, ValueError) as exc:
        typer.echo(f"INVALID: {contract_path}", err=True)
        typer.echo(f"  - cannot read contract JSON: {exc}", err=True)
        raise typer.Exit(code=2)
    return loaded, hashlib.sha256(raw_bytes).hexdigest(), raw


def _verify_contract_signature_or_exit(
    raw: dict, loaded: DeploymentContract, verify_key: Optional[Path]
) -> str:
    """Enforce the approval attestation policy and return a one-line status.

    With a verification key and an approved contract, a valid signature is
    REQUIRED: this is what makes editing JSON to forge an approval fail. With
    no key, verification is skipped (structural validation only) and the
    status names that gap so a deploy gate can insist on a key.
    """
    if verify_key is None:
        if loaded.metadata.status == "approved":
            return (
                "signature: not verified (no --verify-key; structural checks "
                "only, this does not prove authentic approval)"
            )
        return ""
    public = None
    try:
        public = load_public_key(verify_key)
    except AttestationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    if loaded.metadata.status != "approved":
        if contract_has_signature(raw):
            typer.echo(
                "error: a non-approved contract carries an approval signature; "
                "refusing (a draft must not be signed)",
                err=True,
            )
            raise typer.Exit(code=2)
        return "signature: n/a (draft)"
    try:
        verify_contract(raw, public)
    except AttestationError as exc:
        typer.echo(f"attestation refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=2)
    return "signature: VERIFIED against the supplied approval key"


def _resolve_transcript_or_exit(
    contract_path: Path, loaded: DeploymentContract, transcript: Optional[Path]
) -> Transcript:
    """Resolve, authenticate, and parse the transcript; any failure is exit 2."""
    transcript_path = transcript
    if transcript_path is None:
        transcript_path = contract_path.parent / loaded.metadata.transcript
        if not transcript_path.exists():
            typer.echo(
                f"error: declared transcript {loaded.metadata.transcript!r} not found "
                f"at {transcript_path}; provenance cannot be verified. "
                f"Pass --transcript explicitly.",
                err=True,
            )
            raise typer.Exit(code=2)
    elif transcript_path.name != loaded.metadata.transcript:
        typer.echo(
            f"error: --transcript file {transcript_path.name!r} does not match "
            f"metadata.transcript {loaded.metadata.transcript!r}; refusing to verify "
            f"provenance against a different transcript.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        return parse_transcript(transcript_path)
    except (OSError, ValueError) as exc:  # ValueError covers format and UTF-8 errors
        typer.echo(f"INVALID transcript: {exc}", err=True)
        raise typer.Exit(code=2)


def _print_report(report: ValidationReport, contract) -> None:
    meta = contract.metadata
    typer.echo(f"contract: {meta.project} ({meta.status})")
    typer.echo(
        f"requirements: {len(contract.requirements)}  "
        f"open questions: {len(contract.open_questions)}"
    )

    by_id = contract.requirement_by_id()
    if report.conflict_groups:
        typer.echo(f"\nCONFLICTS ({len(report.conflict_groups)}) - resolve before approval:")
        for group in report.conflict_groups:
            typer.echo(f"  {' vs '.join(group)}")
            for req_id in group:
                req = by_id[req_id]
                turns = ", ".join(f"T{n:02d}" for n in req.source_turns)
                typer.echo(f"    {req_id} [{turns}] {req.stakeholder}: {req.title}")

    for question_id in report.open_blocking_questions:
        question = next(q for q in contract.open_questions if q.id == question_id)
        turns = ", ".join(f"T{n:02d}" for n in question.source_turns)
        typer.echo(f"\nBLOCKING OPEN QUESTION {question.id} [{turns}] owner {question.owner}:")
        typer.echo(f"    {question.question}")
    for question_id in report.open_nonblocking_questions:
        typer.echo(f"open question (non-blocking): {question_id}")

    if report.pending_fields:
        typer.echo(f"\npending fields: {', '.join(report.pending_fields)}")

    if report.unwired_requirements:
        typer.echo(
            f"\nADOPTED BUT NOT WIRED IN ({len(report.unwired_requirements)}) - "
            f"every one is a promise the deployment does not keep:"
        )
        for req_id in report.unwired_requirements:
            req = by_id[req_id]
            turns = ", ".join(f"T{n:02d}" for n in req.source_turns) or "follow-up"
            typer.echo(f"  {req_id} [{turns}] {req.stakeholder}: {req.title}")

    if report.errors:
        typer.echo(f"\nSTRUCTURAL ERRORS ({len(report.errors)}):", err=True)
        for error in report.errors:
            typer.echo(f"  - {error}", err=True)
    if report.approval_errors:
        typer.echo(f"\nAPPROVAL VIOLATIONS ({len(report.approval_errors)}):", err=True)
        for error in report.approval_errors:
            typer.echo(f"  - {error}", err=True)

    verdict = {0: "VALID", 1: "APPROVAL BLOCKED", 2: "INVALID"}[report.exit_code]
    typer.echo(f"\nverdict: {verdict}")


@app.command()
def validate(
    contract: Path = typer.Option(..., help="path to a deployment-contract.v1 JSON file"),
    transcript: Optional[Path] = typer.Option(
        None,
        help="transcript to check provenance against; defaults to the file named "
        "by metadata.transcript next to the contract",
    ),
    require_approved: bool = typer.Option(
        False,
        "--require-approved",
        help="exit 1 unless the contract is approved and clean; use in CI/deploy "
        "gates so a draft can never pass",
    ),
    verify_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 public key (PEM); when given, an approved contract must "
        "carry a valid attestation signed by it, otherwise exit 2. This is "
        "what makes a hand-edited approval fail a gate.",
    ),
) -> None:
    """Validate a contract; provenance is always checked against the transcript.

    Without --transcript, the file named by ``metadata.transcript`` is loaded
    from the contract's directory; a missing transcript is a failure, not a
    skipped check. With --verify-key the approval attestation is cryptographically
    verified; structural validation alone cannot prove authentic approval.
    """
    loaded, _sha, raw = _load_contract_snapshot_or_exit(contract)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)
    signature_status = _verify_contract_signature_or_exit(raw, loaded, verify_key)

    report = validate_contract(loaded, parsed_transcript)
    _print_report(report, loaded)
    if signature_status:
        typer.echo(signature_status)
    code = report.exit_code
    if require_approved and code == 0 and loaded.metadata.status != "approved":
        typer.echo(
            "require-approved: contract is a draft; only an approved, clean "
            "contract passes this gate",
            err=True,
        )
        code = 1
    raise typer.Exit(code=code)


@app.command("compile")
def compile_cmd(
    transcript: Path = typer.Option(
        ..., help="discovery transcript with numbered speaker turns"
    ),
    out: Path = typer.Option(..., help="where to write the DRAFT contract JSON"),
    extractor: str = typer.Option(
        "stub",
        help="extraction adapter: 'stub' replays a recorded extraction from "
        "--fixture; 'claude' extracts live with one structured model call "
        "(needs SDK credentials in the environment)",
    ),
    fixture: Optional[Path] = typer.Option(
        None,
        help="recorded extraction result (contract-shaped JSON) for the stub extractor",
    ),
    model: Optional[str] = typer.Option(
        None,
        help=f"model id for the claude extractor (default {DEFAULT_EXTRACTION_MODEL})",
    ),
    force: bool = typer.Option(
        False, "--force", help="overwrite an existing --out file"
    ),
) -> None:
    """Compile a transcript into a DRAFT contract through an extraction adapter.

    The adapter's output is untrusted: the compiled document is always an
    unsigned draft, pinned to the SHA-256 of the parsed transcript, and must
    pass the full validator (including turn provenance) before anything is
    written. Conflicts and open questions in the result are the product
    working; resolve them in the draft, then run ``approve``.
    """
    try:
        parsed_transcript = parse_transcript(transcript)
    except (OSError, ValueError) as exc:  # ValueError covers format and UTF-8 errors
        typer.echo(f"INVALID transcript: {exc}", err=True)
        raise typer.Exit(code=2)

    if extractor == "stub":
        if fixture is None:
            typer.echo(
                "error: the stub extractor replays a recorded extraction; "
                "pass --fixture <extraction.json>",
                err=True,
            )
            raise typer.Exit(code=2)
        if model is not None:
            typer.echo(
                "error: --model only applies to the claude extractor", err=True
            )
            raise typer.Exit(code=2)
        adapter: Extractor = StubExtractor(fixture=fixture)
    elif extractor == "claude":
        if fixture is not None:
            typer.echo(
                "error: the claude extractor reads the transcript itself; "
                "--fixture only applies to the stub extractor",
                err=True,
            )
            raise typer.Exit(code=2)
        adapter = AnthropicExtractor(model=model or DEFAULT_EXTRACTION_MODEL)
    else:
        typer.echo(
            f"error: unknown extractor {extractor!r}; available: stub, claude",
            err=True,
        )
        raise typer.Exit(code=2)

    _refuse_out_aliasing(out, transcript, "transcript (the provenance source)")
    if fixture is not None:
        _refuse_out_aliasing(out, fixture, "extraction fixture")
    if out.exists() and not force:
        _refuse_existing_out(out)

    try:
        document, compiled, report = compile_contract(
            adapter, parsed_transcript, transcript.name
        )
    except ExtractionError as exc:
        typer.echo(f"compile refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)

    _write_contract_or_exit(out, document, force)

    _print_report(report, compiled)
    typer.echo(f"\nwrote {out}")
    # approve and validate resolve metadata.transcript next to the contract;
    # when the draft was written elsewhere, the follow-up command must carry
    # the explicit transcript path or it will not find the provenance source
    transcript_hint = (
        ""
        if out.resolve().parent == transcript.resolve().parent
        else f" --transcript {transcript}"
    )
    typer.echo(
        "next: resolve the conflicts and blocking questions in the draft, then "
        f"sign it off with: discoveryspec approve --contract {out}{transcript_hint} "
        f'--by "<approvers>" --out <approved.json>'
    )
    raise typer.Exit(code=0)


@app.command()
def approve(
    contract: Path = typer.Option(
        ..., help="path to a fully resolved DRAFT deployment-contract.v1 JSON file"
    ),
    by: str = typer.Option(
        ...,
        "--by",
        help='who signs off, e.g. "Anna Lindqvist (Operations), Jonas Weber (Security)"',
    ),
    out: Path = typer.Option(..., help="where to write the approved contract JSON"),
    date: Optional[str] = typer.Option(
        None, help="approval date YYYY-MM-DD; defaults to today"
    ),
    transcript: Optional[Path] = typer.Option(
        None,
        help="transcript to check provenance against; defaults to the file named "
        "by metadata.transcript next to the contract",
    ),
    signing_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 private key (PEM) to cryptographically attest the "
        "approval; without it the approved contract is unsigned and can only "
        "be trusted structurally. Deploy gates should sign and verify.",
    ),
    force: bool = typer.Option(
        False, "--force", help="overwrite an existing --out file"
    ),
) -> None:
    """Stamp a fully resolved draft as approved (the human sign-off step).

    Refuses a draft that still has open conflicts, blocking open questions,
    or unfilled required fields (exit 1), an already-approved contract
    (exit 1), and anything structurally broken (exit 2). On success the
    output pins the SHA-256 of the verified transcript and is guaranteed to
    pass ``validate --require-approved``; only metadata changes, the
    document the human reviewed is otherwise preserved exactly. With
    --signing-key the approval is also Ed25519-signed over a canonical digest
    of the whole contract, so editing any byte later invalidates it.
    """
    try:
        raw = load_contract_raw(contract)
        loaded = contract_from_raw(raw, str(contract))
    except ContractLoadError as exc:
        _echo_load_error_and_exit(exc)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)

    private = None
    if signing_key is not None:
        try:
            private = load_private_key(signing_key)
        except AttestationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)

    _refuse_out_aliasing(
        out, Path(parsed_transcript.path), "verified transcript (the provenance source)"
    )
    _refuse_out_aliasing(out, contract, "reviewed draft contract")
    if signing_key is not None:
        _refuse_out_aliasing(out, signing_key, "signing key")
    if out.exists() and not force:
        _refuse_existing_out(out)

    approved_at = date if date is not None else datetime.date.today().isoformat()
    try:
        stamped = approve_contract(raw, loaded, parsed_transcript, by, approved_at)
    except ApprovalError as exc:
        typer.echo(f"approval refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)

    if private is not None:
        stamped = sign_contract(stamped, private)
    _write_contract_or_exit(out, stamped, force)

    conflict_resolutions = sum(
        1 for r in loaded.requirements if r.conflicts_with and r.resolution is not None
    )
    answered = sum(1 for q in loaded.open_questions if q.status == "resolved")
    typer.echo(f"contract: {loaded.metadata.project}")
    typer.echo(
        f"requirements: {len(loaded.requirements)}  "
        f"conflict resolutions: {conflict_resolutions}  "
        f"questions answered: {answered}"
    )
    for question in loaded.open_questions:
        if question.status == "open" and not question.blocking:
            typer.echo(
                f"open question (non-blocking) stays open past approval: {question.id}"
            )
    typer.echo(f"transcript pinned: sha256 {parsed_transcript.sha256[:12]}...")
    typer.echo(f"approved by: {by} on {approved_at}")
    if private is not None:
        typer.echo("attestation: Ed25519-signed over the full contract digest")
    else:
        typer.echo(
            "attestation: NONE (unsigned; verify with --verify-key requires a "
            "signed contract). Pass --signing-key to make approval tamper-evident."
        )
    typer.echo(f"wrote {out}")
    raise typer.Exit(code=0)


@app.command("export-gate")
def export_gate(
    contract: Path = typer.Option(..., help="path to an APPROVED deployment-contract.v1 JSON file"),
    transcript: Optional[Path] = typer.Option(
        None,
        help="transcript to check provenance against; defaults to the file named "
        "by metadata.transcript next to the contract",
    ),
    out: Path = typer.Option(
        Path("gate-export"), help="output directory for scenarios.yaml and gate-config.json"
    ),
    verify_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 public key (PEM); require a valid approval attestation "
        "before exporting a suite from this contract",
    ),
) -> None:
    """Compile the ten acceptance scenarios into agent-eval-gate artifacts.

    Only an approved, structurally clean contract exports; anything else is
    exit 1 (no unreviewed contract can run). Writes ``scenarios.yaml`` in
    agent-eval-gate's native format (provenance in tags) and
    ``gate-config.json`` with the SLOs and the scenario-to-requirement map.
    """
    loaded, _sha, raw = _load_contract_snapshot_or_exit(contract)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)
    signature_status = _verify_contract_signature_or_exit(raw, loaded, verify_key)
    if signature_status:
        typer.echo(signature_status)

    scenarios_path = out / "scenarios.yaml"
    config_path = out / "gate-config.json"
    try:
        scenarios_payload, config_payload = gate_export(loaded, parsed_transcript)
    except CompileError as exc:
        typer.echo(f"export refused: {exc}", err=True)
        _remove_stale_artifacts(scenarios_path, config_path)
        raise typer.Exit(code=exc.exit_code)

    _write_gate_artifacts(out, scenarios_payload, config_payload)

    count = len(scenarios_payload["scenarios"])
    typer.echo(f"exported {count} scenarios -> {scenarios_path}")
    typer.echo(f"gate config -> {config_path}")
    typer.echo(
        f"run them with: discoveryspec run --contract {contract} "
        "--prices prices.json  (SLO-enforced, provenance-linked verdicts; or feed "
        f"{scenarios_path} to agent-eval-gate directly)"
    )
    raise typer.Exit(code=0)


@app.command()
def run(
    contract: Path = typer.Option(
        ..., help="path to an APPROVED deployment-contract.v1 JSON file"
    ),
    transcript: Optional[Path] = typer.Option(
        None,
        help="transcript to check provenance against; defaults to the file named "
        "by metadata.transcript next to the contract",
    ),
    mode: str = typer.Option(
        "replay",
        help="'replay' executes recorded trajectory fixtures (deterministic, no "
        "API key); 'live' runs the agent module against the Messages API",
    ),
    fixtures: Path = typer.Option(
        Path("fixtures"), help="trajectory fixtures directory (replay mode)"
    ),
    agent: Optional[str] = typer.Option(
        None, help="python module exposing build() -> AgentSpec (live mode)"
    ),
    model: Optional[str] = typer.Option(
        None, help="override the agent model id (live mode)"
    ),
    judge: bool = typer.Option(
        True,
        "--judge/--no-judge",
        help="run the LLM judge on rubric scenarios (live mode only; replay "
        "always skips the judge, matching agent-eval-gate)",
    ),
    judge_model: Optional[str] = typer.Option(
        None, help="judge model id (live mode; defaults to agent-eval-gate's)"
    ),
    prices: Optional[Path] = typer.Option(
        None,
        help='EUR price table JSON, {"<model>": {"input_per_mtok": <eur>, '
        '"output_per_mtok": <eur>}}; required, it verifies the per-invoice '
        "cost SLO",
    ),
    out: Path = typer.Option(
        Path("gate-run"),
        help="output directory for scenarios.yaml, gate-config.json, run.json "
        "(agent-eval-gate format), and run-report.json (provenance-linked); "
        "run artifacts are overwritten on each run",
    ),
    verify_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 public key (PEM); require a valid approval attestation "
        "on the contract before running its suite",
    ),
    signing_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 private key (PEM); attest the run report so a manager "
        "report cannot be rendered from a hand-edited verdict",
    ),
) -> None:
    """Execute the contract's acceptance suite through agent-eval-gate.

    The suite is compiled from the approved contract at run time (never a
    stale export), executed with agent-eval-gate's own loader, adapters, and
    checks, and every verdict is linked back to the requirement it enforces
    and the numbered customer statements behind it. The contract SLOs are
    enforced across the run: statistical p95 latency and the per-invoice cost
    ceiling on every scenario. Fail closed: harness or judge errors exit 2,
    a failing scenario or breached SLO exits 1; there is no non-strict mode.
    """
    scenarios_path = out / "scenarios.yaml"
    config_path = out / "gate-config.json"
    run_json_path = out / "run.json"
    report_json_path = out / "run-report.json"
    artifact_paths = (scenarios_path, config_path, run_json_path, report_json_path)

    loaded, contract_sha256, contract_raw = _load_contract_snapshot_or_exit(contract)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)
    signature_status = _verify_contract_signature_or_exit(contract_raw, loaded, verify_key)

    run_private = None
    if signing_key is not None:
        try:
            run_private = load_private_key(signing_key)
        except AttestationError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)

    # every invocation starts from a clean output directory (no refusal path
    # may leave a previous run's artifacts posing as this run's result), but
    # never at the price of deleting an input that collides with --out
    inputs = [(contract, "contract"), (Path(parsed_transcript.path), "transcript")]
    if prices is not None:
        inputs.append((prices, "price table"))
    if signing_key is not None:
        inputs.append((signing_key, "signing key"))
    if verify_key is not None:
        inputs.append((verify_key, "verification key"))
    for artifact in artifact_paths:
        for input_path, label in inputs:
            _refuse_out_aliasing(artifact, input_path, label)
    _remove_stale_artifacts(*artifact_paths, quiet=True)
    if signature_status:
        typer.echo(signature_status)

    if mode not in ("replay", "live"):
        typer.echo(f"error: --mode must be replay or live, got {mode!r}", err=True)
        raise typer.Exit(code=2)
    if mode == "live" and agent is None:
        typer.echo("error: --agent is required in live mode", err=True)
        raise typer.Exit(code=2)
    if mode == "replay" and (agent is not None or model is not None):
        typer.echo("error: --agent and --model only apply to live mode", err=True)
        raise typer.Exit(code=2)
    if prices is None:
        typer.echo(
            "error: the contract carries a per-invoice cost SLO; pass --prices "
            "with a EUR price table so it can be verified (a run that skips a "
            "promised check proves nothing)",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        price_bytes = prices.read_bytes()
        price_table = json.loads(price_bytes.decode("utf-8"))
        if not isinstance(price_table, dict):
            raise ValueError("price table must be a JSON object keyed by model id")
    except (OSError, ValueError) as exc:
        typer.echo(f"error: cannot read price table {prices}: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        validate_price_table(price_table)
    except RunError as exc:
        typer.echo(f"run refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)
    price_table_sha256 = hashlib.sha256(price_bytes).hexdigest()

    try:
        scenarios_payload, config_payload = gate_export(loaded, parsed_transcript)
    except CompileError as exc:
        typer.echo(f"run refused: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code)

    try:
        gate = require_gate()
    except RunError as exc:
        typer.echo(f"run refused: {exc}", err=True)
        raise typer.Exit(code=exc.exit_code)

    # the gate's own loader reads the export back (what runs is exactly what
    # any agent-eval-gate user would load from these artifacts), but from a
    # staging area: the four visible artifacts commit together at the end, so
    # a failure mid-run leaves the output directory empty (fail closed), never
    # holding a fresh gate export next to a missing or stale run report
    scenarios_text = yaml.safe_dump(
        scenarios_payload, sort_keys=False, allow_unicode=True, width=100
    )
    config_text = json.dumps(config_payload, indent=2, ensure_ascii=False) + "\n"
    staging = out / ".staging"
    staged_scenarios = staging / "scenarios.yaml"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        staged_scenarios.write_text(scenarios_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        typer.echo(f"error: cannot stage gate artifacts under {out}: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        scenarios = gate.load_scenarios(staged_scenarios)
    finally:
        try:
            staged_scenarios.unlink()
            staging.rmdir()
        except OSError as cleanup_exc:
            typer.echo(
                f"warning: could not remove staging files under {staging}: "
                f"{cleanup_exc}",
                err=True,
            )

    use_judge = False
    if mode == "replay":
        adapter = gate.ReplayAdapter(fixtures)
        agent_model = "replay"
    else:
        try:
            spec = gate.load_agent_spec(agent)
        except Exception as exc:
            typer.echo(
                f"error: cannot load agent module {agent!r}: "
                f"{type(exc).__name__}: {exc}",
                err=True,
            )
            raise typer.Exit(code=2)
        if model is not None:
            spec.model = model
        try:
            adapter = gate.AnthropicToolAgent(spec)
        except Exception as exc:
            typer.echo(
                f"error: cannot start the live agent adapter: "
                f"{type(exc).__name__}: {exc}. Live mode needs the SDK dependency "
                f"(e.g. pip install 'agent-eval-gate[anthropic]') and credentials "
                f"in the environment.",
                err=True,
            )
            raise typer.Exit(code=2)
        agent_model = spec.model
        use_judge = judge
        if use_judge and judge_model is None:
            from agent_eval_gate.judge import DEFAULT_JUDGE_MODEL

            judge_model = DEFAULT_JUDGE_MODEL

    try:
        results, judge_errors = execute_scenarios(
            scenarios,
            adapter,
            use_judge=use_judge,
            judge_model=judge_model,
            echo=typer.echo,
        )
        costs = {
            result.scenario_id: scenario_cost_eur(result.trajectory, price_table)
            for result in results
            if result.trajectory.error is None
        }
        clerk_facing_ids = {
            scenario_id
            for scenario_id, prov in config_payload["scenario_provenance"].items()
            if prov.get("clerk_facing", True)
        }
        slo_verdict = evaluate_slos(
            results, costs, config_payload["slo"], clerk_facing_ids
        )
    except RunError as exc:
        typer.echo(f"run refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)

    gate_report = gate.RunReport(
        run_id=time.strftime("%Y%m%d-%H%M%S"),
        agent_model=agent_model,
        judge_model=judge_model if use_judge else None,
        mode=mode,
        results=results,
    )
    report = provenance_report(
        loaded,
        parsed_transcript,
        config_payload,
        results,
        costs,
        slo_verdict,
        mode,
        agent_model,
        run_id=gate_report.run_id,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        contract_sha256=contract_sha256,
        judge_error_ids=judge_errors,
    )
    run_json_text = gate_report.model_dump_json(indent=2)
    report["provenance"] = _run_provenance(gate, price_table_sha256, prices.name)
    # bind the raw gate report into the summary, so a signed run-report.json
    # also authenticates the run.json evidence next to it
    report["provenance"]["run_json_sha256"] = hashlib.sha256(
        run_json_text.encode("utf-8")
    ).hexdigest()
    if run_private is not None:
        report = sign_run(report, run_private)
    _write_all_atomic(out, {
        "scenarios.yaml": scenarios_text,
        "gate-config.json": config_text,
        "run.json": run_json_text,
        "run-report.json": json.dumps(report, indent=2, ensure_ascii=False) + "\n",
    })

    typer.echo("")
    _echo_any_console("\n".join(console_lines(report)))
    if run_private is not None:
        typer.echo("run report attestation: Ed25519-signed")
    typer.echo(f"gate run report -> {run_json_path}")
    typer.echo(f"provenance report -> {report_json_path}")

    code = run_exit_code(results, judge_errors, slo_verdict)
    if code == 2:
        typer.echo(
            "run incomplete (harness or judge errors); exiting 2 fail closed, "
            "this run proves nothing",
            err=True,
        )
    raise typer.Exit(code=code)


@app.command()
def report(
    contract: Path = typer.Option(
        ..., help="the APPROVED contract the run executed (verified by hash)"
    ),
    run: Path = typer.Option(
        Path("gate-run"),
        help="run output directory containing run-report.json",
    ),
    brand: Optional[Path] = typer.Option(
        None,
        help="brand.v1 JSON file: identity, palette, font stacks, and hard "
        "style policy (shadows, gradients, corner radius, emoji); defaults "
        "to the built-in neutral brand",
    ),
    out: Path = typer.Option(
        Path("report.html"), help="where to write the self-contained HTML report"
    ),
    verify_key: Optional[Path] = typer.Option(
        None,
        help="Ed25519 public key (PEM); require valid attestations on BOTH the "
        "contract and the run report before rendering. Without it, a "
        "hand-edited verdict could render as a false 'Cleared to deploy'.",
    ),
    force: bool = typer.Option(
        False, "--force", help="overwrite an existing --out file"
    ),
) -> None:
    """Render an acceptance run as a manager-facing HTML page.

    One self-contained page for a non-technical decision maker: the verdict,
    every broken promise with the stakeholder's quoted words, all scenarios
    in plain language, and the agreed limits versus what was observed. The
    report refuses to render when the contract is not the one the run
    executed, when the brand palette fails WCAG AA contrast, or when any
    brand style rule would be violated. Runs with harness errors render as
    INCOMPLETE, never as a plain pass or fail. With --verify-key both the
    contract and the run report must carry valid attestations, so a forged
    verdict cannot be rendered.
    """
    loaded, contract_sha256, contract_raw = _load_contract_snapshot_or_exit(contract)
    signature_status = _verify_contract_signature_or_exit(contract_raw, loaded, verify_key)
    run_report_path = run / "run-report.json"
    try:
        run_report = json.loads(run_report_path.read_text(encoding="utf-8"))
        if not isinstance(run_report, dict):
            raise ValueError("run-report.json must be a JSON object")
    except (OSError, ValueError) as exc:
        typer.echo(f"error: cannot read {run_report_path}: {exc}", err=True)
        raise typer.Exit(code=2)

    if verify_key is not None:
        try:
            public = load_public_key(verify_key)
            if not run_has_signature(run_report):
                raise AttestationError(
                    "run report carries no attestation; a report can only be "
                    "trusted when the run was signed (run --signing-key)"
                )
            verify_run(run_report, public)
        except AttestationError as exc:
            typer.echo(f"report refused: {exc}", err=True)
            for problem in exc.problems:
                typer.echo(f"  - {problem}", err=True)
            raise typer.Exit(code=2)

    # cross-check the summarized report against the raw gate evidence: the
    # signed summary pins run.json's hash, so a swapped or edited run.json
    # cannot sit next to an authentic-looking run-report.json
    pinned_run_json = (run_report.get("provenance") or {}).get("run_json_sha256")
    if pinned_run_json is not None:
        run_json_path = run / "run.json"
        try:
            actual_run_json = hashlib.sha256(run_json_path.read_bytes()).hexdigest()
        except OSError as exc:
            if verify_key is not None:
                typer.echo(
                    f"report refused: run-report.json pins run.json "
                    f"({pinned_run_json}) but the raw gate report cannot be "
                    f"read: {exc}",
                    err=True,
                )
                raise typer.Exit(code=2)
            actual_run_json = None
        if actual_run_json is not None and actual_run_json != pinned_run_json:
            typer.echo(
                "report refused: run.json does not match the hash pinned in "
                "run-report.json; the raw gate evidence was modified or "
                "replaced after the run",
                err=True,
            )
            raise typer.Exit(code=2)

    try:
        brand_config = load_brand(brand) if brand is not None else DEFAULT_BRAND
    except BrandError as exc:
        typer.echo(f"report refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)

    _refuse_out_aliasing(out, contract, "contract")
    _refuse_out_aliasing(out, run_report_path, "run report")
    if brand is not None:
        _refuse_out_aliasing(out, brand, "brand file")
    if verify_key is not None:
        _refuse_out_aliasing(out, verify_key, "verification key")
    if out.exists() and not force:
        _refuse_existing_out(out)

    try:
        html = render_report(loaded, run_report, brand_config, contract_sha256)
    except (ReportError, BrandError) as exc:
        typer.echo(f"report refused: {exc}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=exc.exit_code)

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(html)
    except FileExistsError:
        _refuse_existing_out(out)
    except OSError as exc:
        typer.echo(f"error: cannot write {out}: {exc}", err=True)
        raise typer.Exit(code=2)

    view_verdict = run_report.get("verdict", "")
    typer.echo(f"verdict: {view_verdict}")
    if signature_status:
        typer.echo(signature_status)
    if verify_key is not None:
        typer.echo("run report attestation: VERIFIED")
    typer.echo(f"report -> {out}")
    typer.echo(
        f"style: {brand_config.identity.company} "
        f"(shadows {'allowed' if brand_config.policy.allow_shadows else 'forbidden'}, "
        f"corner radius max {brand_config.policy.max_corner_radius_px}px)"
    )
    raise typer.Exit(code=0)


@app.command()
def keygen(
    out: Path = typer.Option(
        ...,
        help="path prefix for the key pair; writes <out>.pem (private key) and "
        "<out>.pub (public key)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="replace an existing key pair at this path; everything signed with "
        "the old key stops verifying",
    ),
) -> None:
    """Generate the Ed25519 key pair the signed workflow runs on.

    Writes an unencrypted PKCS8 private key to ``<out>.pem``, created
    owner-readable where the platform enforces file modes, and the matching
    public key to ``<out>.pub``. The private key is the entire trust anchor of
    this pipeline: whoever holds it can mint approvals that every downstream
    gate accepts. Keep it out of the repository and hand out only the ``.pub``.
    """
    import os

    private_path = out.parent / (out.name + ".pem")
    public_path = out.parent / (out.name + ".pub")

    existing = [str(p) for p in (private_path, public_path) if p.exists()]
    if existing and not force:
        typer.echo(
            f"error: {', '.join(existing)} already exists; refusing to replace a "
            f"signing key, because every contract and run report signed with it "
            f"would stop verifying. Choose another --out, or pass --force if the "
            f"existing key is genuinely retired.",
            err=True,
        )
        raise typer.Exit(code=2)

    private_pem, public_pem = generate_keypair()
    try:
        private_path.parent.mkdir(parents=True, exist_ok=True)
        # exclusive create with an owner-only mode: a private key must never be
        # written world-readable, and a check-then-write would leave a window
        # for a hostile file to be pre-created at this path
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
        handle = os.open(private_path, flags, 0o600)
        try:
            os.write(handle, private_pem)
        finally:
            os.close(handle)
    except FileExistsError:
        _refuse_existing_out(private_path)
    except OSError as exc:
        typer.echo(f"error: cannot write {private_path}: {exc}", err=True)
        raise typer.Exit(code=2)

    try:
        public_path.write_bytes(public_pem)
    except OSError as exc:
        # never leave a private key on disk whose public half is missing: the
        # operator would have no way to verify what it signs
        try:
            private_path.unlink()
        except OSError:
            typer.echo(
                f"error: could not remove {private_path} after the failure; "
                f"delete it by hand before using this path again",
                err=True,
            )
        typer.echo(f"error: cannot write {public_path}: {exc}", err=True)
        raise typer.Exit(code=2)

    fingerprint = public_key_fingerprint(load_public_key(public_path))
    typer.echo(f"private key -> {private_path}  (secret; never commit it)")
    typer.echo(f"public key  -> {public_path}")
    typer.echo(f"fingerprint: {fingerprint}")
    typer.echo(
        f"sign with:   discoveryspec approve --contract <resolved.json> "
        f'--by "<approvers>" --out <approved.json> --signing-key {private_path}'
    )
    typer.echo(
        f"verify with: discoveryspec validate --contract <approved.json> "
        f"--require-approved --verify-key {public_path}"
    )
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
