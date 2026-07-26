"""The schema an adapter declares about the store it captured.

StateDiff hardcoded one enterprise's tables, primary keys, columns, and event
types as module-level dicts. BlastRadius moves that knowledge behind the
adapter: every adapter (SQL, a JSON snapshot, SiloBench) declares its own Schema,
and the engine, diff, and rules read the schema off the captured state instead of
a global. This is the single inversion that makes the oracle framework-neutral.

The schema is also what makes selector validation fail closed: a contract that
names a table or column the store does not have is rejected as an `error`, never
silently treated as matching nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Schema:
    # table -> its primary key column. The diff keys rows by this, and every rule
    # that names a row resolves through it.
    primary_keys: dict[str, str]
    # table -> the exact set of column names a row may carry. Selectors and
    # row/field rules validate against this; an unknown column is an error.
    columns: dict[str, frozenset[str]]
    # known event types, or None to accept any type (an open audit vocabulary).
    # When a set is given, an event rule naming a type outside it is an error.
    event_types: frozenset[str] | None = None
    # tables excluded from the generic row diff: the events table (represented as
    # appended-event atoms plus the append-only invariant) and any bookkeeping
    # tables governed by adapter consistency invariants rather than the diff.
    non_diffed_tables: frozenset[str] = field(default_factory=frozenset)
    # the table that holds the events, if events live in a table in the capture
    # (SiloBench does; a SQL adapter may source events elsewhere). Informational.
    events_table: str | None = None
    # a config/profile table whose changes may be waived by profile_change=allow,
    # or None if the store has no such table.
    environment_table: str | None = None
    # a label for the schema, recorded in the verdict fingerprints.
    name: str = "state"

    def has_table(self, table: str) -> bool:
        return table in self.columns

    def primary_key(self, table: str) -> str:
        return self.primary_keys[table]
