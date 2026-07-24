"""The `blast` command line.

`blast check` grades an already-captured before/after pair against a contract and
exits 0 (pass), 1 (fail), or 2 (error, an unjudgeable run), so a CI step can gate
on it directly. `blast explain` prints a contract in plain language.

Live SQL capture (snapshot a database around an agent run) is the library and
pytest path, not a CLI command: see blastradius.capture and the pytest plugin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

try:
    import typer
except ModuleNotFoundError:  # pragma: no cover
    raise SystemExit(
        "the blast CLI needs the 'cli' extra: pip install 'blastradius[cli]'"
    )

from .adapter import ArtifactError, ArtifactPair
from .engine import evaluate
from .scenario import Scenario, ScenarioError, load_scenario

app = typer.Typer(add_completion=False, help="Grade what your agent changed in the world.")

# status -> process exit code. Mirrors a state gate: pass clears, fail blocks,
# error (unjudgeable) blocks exactly like fail.
_EXIT = {"pass": 0, "fail": 1, "error": 2}


def _load_pair(adapter: str, before: str, after: str,
               before_events: Optional[str], after_events: Optional[str]) -> ArtifactPair:
    """Resolve the named adapter and load the pair. Optional dependencies stay
    optional: the adapter module is imported only when its name is asked for."""
    if adapter == "snapshot":
        from .adapters.snapshot import SnapshotAdapter
        return SnapshotAdapter().load_pair(before, after)
    if adapter == "silobench":
        from .adapters.silobench import SilobenchAdapter
        return SilobenchAdapter().load_pair(before, after, before_events, after_events)
    if adapter == "sql":
        raise typer.BadParameter(
            "the sql adapter captures a live database; use blastradius.capture in code "
            "or the pytest plugin, not `blast check`"
        )
    raise typer.BadParameter(f"unknown adapter {adapter!r} (known: snapshot, silobench, sql)")


@app.command()
def check(
    contract: Path = typer.Option(..., "--contract", "-c", help="Contract file (blastradius.contract.v1)."),
    before: str = typer.Option(..., "--before", help="Before capture (path for file adapters)."),
    after: str = typer.Option(..., "--after", help="After capture."),
    adapter: str = typer.Option("snapshot", "--adapter", "-a", help="snapshot | silobench."),
    before_events: Optional[str] = typer.Option(None, "--before-events", help="Optional before event log."),
    after_events: Optional[str] = typer.Option(None, "--after-events", help="Optional after event log."),
    as_json: bool = typer.Option(False, "--json", help="Print the full verdict as JSON."),
    gate: bool = typer.Option(False, "--gate", help="Print gate-style {name,passed,detail} checks as JSON."),
) -> None:
    """Grade a captured before/after pair against a contract. Exits 0/1/2."""
    try:
        parsed: Scenario = load_scenario(contract)
    except ScenarioError as exc:
        typer.echo(f"contract error: {exc}", err=True)
        raise typer.Exit(2)

    # An artifact problem is a fail-closed `error`, never an exception that
    # escapes as a crash: build the error verdict the same way the engine does.
    try:
        pair = _load_pair(adapter, before, after, before_events, after_events)
    except ArtifactError as exc:
        from .engine import error_verdict
        verdict = error_verdict(parsed.id, str(exc), title=parsed.title)
    else:
        verdict = evaluate(parsed, pair)

    if as_json:
        typer.echo(verdict.to_json())
    elif gate:
        import json
        typer.echo(json.dumps(verdict.to_gate_checks(), ensure_ascii=False, indent=2))
    else:
        typer.echo(verdict.report())
    raise typer.Exit(_EXIT[verdict.status])


@app.command()
def explain(
    contract: Path = typer.Option(..., "--contract", "-c", help="Contract file to describe."),
) -> None:
    """Print a contract's effects in plain language."""
    try:
        parsed = load_scenario(contract)
    except ScenarioError as exc:
        typer.echo(f"contract error: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"{parsed.id}: {parsed.title}")
    for list_name in ("expected", "allowed", "forbidden"):
        effects = parsed.by_list(list_name)
        if not effects:
            continue
        typer.echo(f"\n{list_name} ({len(effects)}):")
        for effect in effects:
            table = getattr(effect.spec, "table", None) or getattr(effect.spec, "type", "")
            typer.echo(f"  - {effect.rule}[{effect.spec.id}] {table}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
