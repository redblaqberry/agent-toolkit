"""discoveryspec CLI.

Current surface: ``validate`` and ``export-gate``. The compile (LLM
extraction), approve, and run commands build on top of this validator.

Exit codes (fail closed, same convention as agent-eval-gate):
  0  structurally valid; draft findings (conflicts, open questions, pending
     fields) are reported, they are the product working as intended
  1  approval violation: the contract claims approved but has open conflicts,
     blocking open questions, missing sign-off, or unfilled required fields;
     export-gate also exits 1 for any contract that is not approved and clean
  2  structural failure: unreadable file, schema or model violation, broken
     provenance, dangling references, malformed transcript
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
import yaml

from .loader import ContractLoadError, load_contract
from .models import DeploymentContract
from .scenarios import CompileError, gate_export
from .transcript import Transcript, parse_transcript
from .validate import ValidationReport, validate_contract

app = typer.Typer(add_completion=False)


@app.callback()
def main() -> None:
    """Compile discovery transcripts into reviewed, executable deployment contracts."""


def _load_contract_or_exit(contract_path: Path) -> DeploymentContract:
    try:
        return load_contract(contract_path)
    except ContractLoadError as exc:
        typer.echo(f"INVALID: {exc.path}", err=True)
        for problem in exc.problems:
            typer.echo(f"  - {problem}", err=True)
        raise typer.Exit(code=2)


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
) -> None:
    """Validate a contract; provenance is always checked against the transcript.

    Without --transcript, the file named by ``metadata.transcript`` is loaded
    from the contract's directory; a missing transcript is a failure, not a
    skipped check.
    """
    loaded = _load_contract_or_exit(contract)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)

    report = validate_contract(loaded, parsed_transcript)
    _print_report(report, loaded)
    code = report.exit_code
    if require_approved and code == 0 and loaded.metadata.status != "approved":
        typer.echo(
            "require-approved: contract is a draft; only an approved, clean "
            "contract passes this gate",
            err=True,
        )
        code = 1
    raise typer.Exit(code=code)


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
) -> None:
    """Compile the ten acceptance scenarios into agent-eval-gate artifacts.

    Only an approved, structurally clean contract exports; anything else is
    exit 1 (no unreviewed contract can run). Writes ``scenarios.yaml`` in
    agent-eval-gate's native format (provenance in tags) and
    ``gate-config.json`` with the SLOs and the scenario-to-requirement map.
    """
    loaded = _load_contract_or_exit(contract)
    parsed_transcript = _resolve_transcript_or_exit(contract, loaded, transcript)

    scenarios_path = out / "scenarios.yaml"
    config_path = out / "gate-config.json"
    try:
        scenarios_payload, config_payload = gate_export(loaded, parsed_transcript)
    except CompileError as exc:
        typer.echo(f"export refused: {exc}", err=True)
        # a refused export must not leave a previously exported suite behind
        for stale in (scenarios_path, config_path):
            if stale.exists():
                stale.unlink()
                typer.echo(f"removed stale artifact {stale}", err=True)
        raise typer.Exit(code=exc.exit_code)

    out.mkdir(parents=True, exist_ok=True)
    scenarios_path.write_text(
        yaml.safe_dump(scenarios_payload, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(config_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    count = len(scenarios_payload["scenarios"])
    typer.echo(f"exported {count} scenarios -> {scenarios_path}")
    typer.echo(f"gate config -> {config_path}")
    typer.echo(
        "run them with: agent-eval-gate run --scenarios "
        f"{scenarios_path} --agent <module> --mode live --strict "
        "--prices prices.json  (strict, so failed scenarios fail the exit code; "
        "prices enable the per-invoice cost verdict against gate-config.json)"
    )
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
