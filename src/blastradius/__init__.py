"""BlastRadius: assert what your agent must and must not change in the world.

A framework-neutral, fail-closed state oracle for agent side effects. Capture
your store before and after an agent runs, declare a contract of expected,
allowed, and forbidden effects, and grade what actually changed. Every change
must be justified by an effect (default-deny), and an unjudgeable run returns
`error` rather than guessing (fail-closed), so it blocks a release gate exactly
like a failure.

The engine is store-agnostic: adapters (SQL, a JSON snapshot, SiloBench) each
declare their own Schema and hand the engine a normalized before/after pair.
"""

from __future__ import annotations

from .adapter import (
    ArtifactError,
    ArtifactPair,
    CaptureAdapter,
    NormalizedSnapshot,
    StateAdapter,
)
from .api import Capture, capture, grade
from .engine import evaluate
from .gate import to_gate_checks
from .models import CheckOutcome, EventRecord, Verdict
from .scenario import Scenario, ScenarioError, load_scenario, parse_scenario
from .schema import Schema

__version__ = "0.1.0"

# `Contract` is the public name for a parsed contract; internally the engine
# reads it as `Scenario` (the type ported from StateDiff). They are the same
# object; the alias is the vocabulary the CLI and docs use.
Contract = Scenario

__all__ = [
    "__version__",
    "evaluate",
    "grade",
    "capture",
    "Capture",
    "Contract",
    "Scenario",
    "ScenarioError",
    "load_scenario",
    "parse_scenario",
    "Verdict",
    "CheckOutcome",
    "EventRecord",
    "Schema",
    "NormalizedSnapshot",
    "ArtifactPair",
    "ArtifactError",
    "StateAdapter",
    "CaptureAdapter",
    "to_gate_checks",
]
