"""The adapter boundary: what every state store must present to the engine.

An adapter parses or captures its own store (a SiloBench snapshot, a JSON
export, a live SQL database) and hands the engine a NormalizedSnapshot: the
tables keyed for diffing, the parsed domain events, the raw event rows for the
append-only invariant, an identity hash, and the Schema it declares. The engine,
diff, and rules never know which store they came from.

Everything an adapter does fails closed: any structural surprise, hash mismatch,
or internal inconsistency raises ArtifactError, which the engine reports as an
`error` verdict, never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .models import EventRecord, Row
from .schema import Schema


class ArtifactError(ValueError):
    """The captured state cannot be trusted; evaluation must return `error`."""


@dataclass(frozen=True)
class NormalizedSnapshot:
    """One captured state, normalized to what the engine reads. Produced by an
    adapter from its native format; consumed by diff.py and the rules."""

    schema: Schema
    tables: dict[str, list[Row]]
    events: list[EventRecord]
    # the raw stored event rows, exactly as captured. The append-only invariant
    # compares THESE, so a reformatted or reordered historical event is a rewrite
    # even when the parsed event object would compare equal.
    raw_events: list[Row]
    state_hash: str
    source: str = ""

    def primary_keys(self) -> dict[str, str]:
        return self.schema.primary_keys


@dataclass(frozen=True)
class ArtifactPair:
    before: NormalizedSnapshot
    after: NormalizedSnapshot
    # Whether each captured state's events were cross-checked against an
    # independent event log. The logs are optional inputs, so False means the
    # check never ran, which is NOT the same as passing it. Defaults are False so
    # any pair built without stating otherwise claims nothing.
    before_events_cross_checked: bool = False
    after_events_cross_checked: bool = False

    def __post_init__(self) -> None:
        # The two states must be graded under one schema, or the diff keys rows by
        # a primary key one side does not have. An adapter that produced mismatched
        # schemas is an internal inconsistency, so it fails closed.
        if self.before.schema.primary_keys != self.after.schema.primary_keys:
            raise ArtifactError("before and after were captured under different schemas")


@runtime_checkable
class StateAdapter(Protocol):
    """Loads an already-captured before/after pair from this store's format.

    Live stores (SQL) additionally implement `capture()` to snapshot the current
    state in place; see CaptureAdapter.
    """

    name: str

    def load_pair(
        self,
        before,
        after,
        before_events=None,
        after_events=None,
    ) -> ArtifactPair:
        ...


@runtime_checkable
class CaptureAdapter(StateAdapter, Protocol):
    """A store that can be snapshotted live, so the harness can capture the
    before state, let the agent run, and capture the after state in one process."""

    def capture(self, label: str = "") -> NormalizedSnapshot:
        ...
