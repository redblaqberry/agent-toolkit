"""Generic snapshot adapter: load a store-neutral JSON capture pair.

This is the adapter for a team that has no purpose-built adapter: dump your
state to one self-describing JSON object per capture and BlastRadius can grade
it. No SQL, no SiloBench, no code. A `blastradius.snapshot.v1` file carries its
own schema, its tables, and its event stream, so the engine reads it the same
way it reads any other adapter's output.

Everything here fails closed. Any structural surprise, a duplicate JSON key, a
value outside the JSON/Unicode domain, a row whose columns disagree with the
declared schema, or a state_hash that does not recompute raises ArtifactError,
which the engine reports as an `error` verdict (never a pass).

The `blastradius.snapshot.v1` format (one JSON object):

    {
      "format": "blastradius.snapshot.v1",
      "schema": {
        "name": "orders",
        "primary_keys": {"orders": "order_id", "events": "event_id"},
        "columns": {"orders": ["order_id", "status", "total"],
                    "events": ["event_id", "type", "payload"]},
        "event_types": ["ORDER_PLACED", "ORDER_SHIPPED"],  // optional; omit/null = any type
        "non_diffed_tables": ["events"],                   // optional; default {events_table}
        "events_table": "events",                          // optional
        "environment_table": null                          // optional
      },
      "tables": {"orders": [{"order_id": "O1", "status": "placed", "total": 100}],
                 "events": [{"event_id": "E1", "type": "ORDER_PLACED", "payload": {}}]},
      "events": [{"event_id": "E1", "mutation_seq": 1, "ts": "...", "actor": "a",
                  "system": "s", "type": "ORDER_PLACED", "entity_type": "order",
                  "entity_id": "O1", "payload": {}}],
      "state_hash": "..."   // optional; if present it must recompute, else it is computed
    }

Two representations of the event stream, chosen by whether `events_table` is set:

  - `events` (the parsed EventRecords the rules read) ALWAYS come from the
    top-level "events" list, which is the only place that carries the full
    EventRecord fields (mutation_seq, ts, actor, system, entity, payload).
  - `raw_events` (what the append-only invariant compares) is:
      * the raw rows of the declared `events_table`, when one is set. Those rows
        are the hashed, table-shaped form of the log; they are required to be
        1:1 with the "events" list in the same order (same event_id at each
        index), because the engine reads the appended suffix as
        `after.events[len(before.raw_events):]`, and that slice is correct only
        when the two representations are the same stream in the same order.
      * the top-level "events" list verbatim, when no `events_table` is set. The
        parsed events come from that same list, so they align by construction.

The events table is exempt from the scalar-cell check every other table gets:
its rows carry a nested `payload` object, which is legitimate there and nowhere
else. Payloads are still hashed as canonical JSON, which admits text, integers,
booleans, null, and nested objects/arrays, but not floats (the v1 canonical
domain has no float form).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..adapter import ArtifactError, ArtifactPair, NormalizedSnapshot
from ..canonical import CanonicalError, canonical_json, sha256_hex
from ..models import EventRecord, Row
from ..schema import Schema

FORMAT = "blastradius.snapshot.v1"
ADAPTER_NAME = "snapshot"

_TOP_LEVEL_KEYS = frozenset({"format", "schema", "tables", "events", "state_hash"})
_SCHEMA_KEYS = frozenset({
    "name", "primary_keys", "columns", "event_types",
    "non_diffed_tables", "events_table", "environment_table",
})

__all__ = ["SnapshotAdapter", "load_snapshot", "load_pair", "FORMAT", "ADAPTER_NAME"]


# --- fail-closed JSON decoding (borrowed from the SiloBench adapter's rigor) ---

def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.loads' object hook: a repeated object key is a hard error.

    The default keeps the last value and drops the earlier one silently, which
    is invisible here because the state_hash is computed from the PARSED
    structure: two byte-different files that differ only in a duplicated key
    canonicalize identically, so the hash comparison cannot see the rewrite.
    """
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ArtifactError(f"duplicate JSON object key {key!r}")
        seen[key] = value
    return seen


def _require_json_domain(what: str, value: Any) -> None:
    """Refuse anything Python's json module accepts that JSON itself does not.

    json.loads decodes NaN/Infinity by default, and a decoded lone surrogate has
    no UTF-8 encoding at all. NaN is not equal to itself, so a non-finite number
    that reached a verdict would make every comparison against it answer "these
    differ", indistinguishable from a real disagreement; a lone surrogate
    poisons every message that quotes it, so a refusal built from it cannot be
    written to stdout. Neither can appear in a v1 capture.
    """
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ArtifactError(f"{what} outside the JSON/Unicode domain: {exc}") from exc


def _strict_json_loads(text: str, source: str) -> Any:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ArtifactError as exc:
        raise ArtifactError(f"{source}: {exc}") from exc
    _require_json_domain(source, value)
    return value


def _read_json(path: Path) -> Any:
    try:
        return _strict_json_loads(path.read_text(encoding="utf-8"), str(path))
    except FileNotFoundError as exc:
        raise ArtifactError(f"capture not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"unparseable capture {path}: {exc}") from exc
    except RecursionError as exc:
        # A document nested thousands of arrays deep exhausts the parser stack.
        # RecursionError is not a JSONDecodeError, so without this it escapes as
        # a traceback and exit 1 where a malformed capture must become `error`.
        raise ArtifactError(f"unparseable capture {path}: JSON nested too deeply to parse") from exc


# --- schema, tables, rows, events ---------------------------------------------

def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], source: str, what: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ArtifactError(f"{source}: unknown {what} key(s) {unknown}; allowed {sorted(allowed)}")


def _require_str(source: str, where: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactError(f"{source}: {where} must be text, got {value!r} ({type(value).__name__})")
    return value


def _build_schema(raw: Any, source: str) -> Schema:
    """Build a Schema from the capture's "schema" block, fail-closed."""
    if not isinstance(raw, dict):
        raise ArtifactError(f"{source}: 'schema' must be an object, got {type(raw).__name__}")
    _reject_unknown_keys(raw, _SCHEMA_KEYS, source, "schema")

    name = raw.get("name", "state")
    _require_str(source, "schema.name", name)

    primary_keys = raw.get("primary_keys")
    if not isinstance(primary_keys, dict) or not primary_keys:
        raise ArtifactError(f"{source}: 'schema.primary_keys' must be a non-empty object of table -> pk column")
    for table, pk in primary_keys.items():
        _require_str(source, f"schema.primary_keys[{table!r}]", pk)

    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, dict):
        raise ArtifactError(f"{source}: 'schema.columns' must be an object of table -> [column]")
    columns: dict[str, frozenset[str]] = {}
    for table, cols in columns_raw.items():
        if not isinstance(cols, list) or not cols:
            raise ArtifactError(f"{source}: schema.columns.{table} must be a non-empty list of column names")
        for col in cols:
            _require_str(source, f"schema.columns.{table}[]", col)
        if len(cols) != len(set(cols)):
            raise ArtifactError(f"{source}: schema.columns.{table} lists a column twice: {cols}")
        columns[table] = frozenset(cols)

    if set(primary_keys) != set(columns):
        raise ArtifactError(
            f"{source}: schema primary_keys and columns name different tables "
            f"(primary_keys {sorted(primary_keys)}, columns {sorted(columns)})"
        )
    for table, pk in primary_keys.items():
        if pk not in columns[table]:
            raise ArtifactError(
                f"{source}: schema primary key {pk!r} is not a column of {table!r} ({sorted(columns[table])})"
            )

    event_types_raw = raw.get("event_types")
    if event_types_raw is None:
        event_types: frozenset[str] | None = None
    elif isinstance(event_types_raw, list):
        for et in event_types_raw:
            _require_str(source, "schema.event_types[]", et)
        event_types = frozenset(event_types_raw)
    else:
        raise ArtifactError(f"{source}: 'schema.event_types' must be a list of type names or null")

    events_table = raw.get("events_table")
    if events_table is not None:
        _require_str(source, "schema.events_table", events_table)
        if events_table not in primary_keys:
            raise ArtifactError(f"{source}: schema.events_table {events_table!r} is not a declared table")

    environment_table = raw.get("environment_table")
    if environment_table is not None:
        _require_str(source, "schema.environment_table", environment_table)
        if environment_table not in primary_keys:
            raise ArtifactError(f"{source}: schema.environment_table {environment_table!r} is not a declared table")

    non_diffed_raw = raw.get("non_diffed_tables")
    if non_diffed_raw is None:
        # Default: the events table is represented as event atoms plus the
        # append-only invariant, never the generic row diff. With no events
        # table there is nothing to exclude.
        non_diffed = frozenset({events_table}) if events_table is not None else frozenset()
    elif isinstance(non_diffed_raw, list):
        for tbl in non_diffed_raw:
            _require_str(source, "schema.non_diffed_tables[]", tbl)
            if tbl not in primary_keys:
                raise ArtifactError(f"{source}: schema.non_diffed_tables names undeclared table {tbl!r}")
        non_diffed = frozenset(non_diffed_raw)
    else:
        raise ArtifactError(f"{source}: 'schema.non_diffed_tables' must be a list of table names or null")

    return Schema(
        primary_keys=dict(primary_keys),
        columns=columns,
        event_types=event_types,
        non_diffed_tables=non_diffed,
        events_table=events_table,
        environment_table=environment_table,
        name=name,
    )


def _verify_tables(schema: Schema, tables: Any, source: str) -> None:
    if not isinstance(tables, dict):
        raise ArtifactError(f"{source}: 'tables' must be an object of table -> rows, got {type(tables).__name__}")
    expected = set(schema.primary_keys)
    actual = set(tables)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ArtifactError(f"{source}: tables do not match schema (missing {missing}, unknown {unknown})")


def _verify_rows(schema: Schema, tables: dict[str, list[Row]], source: str) -> None:
    events_table = schema.events_table
    for table, pk in schema.primary_keys.items():
        rows = tables[table]
        if not isinstance(rows, list):
            raise ArtifactError(f"{source}: tables.{table} must be a list of rows, got {type(rows).__name__}")
        expected_columns = schema.columns[table]
        is_events = events_table is not None and table == events_table
        seen: set[Any] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ArtifactError(f"{source}: tables.{table} has a non-object row {row!r}")
            if set(row) != expected_columns:
                raise ArtifactError(
                    f"{source}: {table} row columns {sorted(row)} != declared columns {sorted(expected_columns)}"
                )
            for column, cell in row.items():
                if column == pk:
                    # The primary key is the row's identity: the diff keys rows
                    # by it and every rule that names a row resolves through it,
                    # so it must be a non-null scalar even in the events table.
                    if cell is None:
                        raise ArtifactError(f"{source}: {table} has a row with a null {pk}")
                    if isinstance(cell, bool) or not isinstance(cell, (str, int)):
                        raise ArtifactError(
                            f"{source}: {table}.{pk} is {cell!r} ({type(cell).__name__}); "
                            "a primary key is text or an integer"
                        )
                elif not is_events:
                    # A validated cell holds only text, an integer, or null, the
                    # engine's canonical domain. A JSON boolean collides with an
                    # integer under Python equality while hashing differently, and
                    # a float or nested value has no cell form at all.
                    if isinstance(cell, bool) or not isinstance(cell, (str, int, type(None))):
                        raise ArtifactError(
                            f"{source}: {table}.{column} is {cell!r} ({type(cell).__name__}); "
                            "a snapshot cell holds only text, an integer, or null"
                        )
                # else: an events-table non-pk cell (the payload) may be any JSON.
            value = row[pk]
            if value in seen:
                raise ArtifactError(f"{source}: {table} has duplicate primary key {value!r}")
            seen.add(value)


def _parse_events(events_list: Any, source: str) -> list[EventRecord]:
    if not isinstance(events_list, list):
        raise ArtifactError(f"{source}: 'events' must be a list, got {type(events_list).__name__}")
    events: list[EventRecord] = []
    for index, item in enumerate(events_list):
        if not isinstance(item, dict):
            raise ArtifactError(f"{source}: events[{index}] is not an object: {item!r}")
        try:
            events.append(EventRecord.model_validate(item))
        except ValidationError as exc:
            raise ArtifactError(f"{source}: events[{index}] is not a valid event: {exc}") from exc
    for previous, current in zip(events, events[1:]):
        if current.mutation_seq < previous.mutation_seq:
            raise ArtifactError(
                f"{source}: event {current.event_id} has mutation_seq {current.mutation_seq} "
                f"below its predecessor {previous.mutation_seq}; the log ordering contract is broken"
            )
    return events


def _resolve_raw_events(
    schema: Schema, tables: dict[str, list[Row]], events_list: list[Any], events: list[EventRecord], source: str
) -> list[Row]:
    """The rows the append-only invariant compares. See the module docstring for
    why the two representations must be the same stream in the same order."""
    if schema.events_table is None:
        # No events table: the invariant compares the "events" list verbatim, and
        # the parsed events came from that same list, so they align by construction.
        return list(events_list)
    raw_events = tables[schema.events_table]
    # The engine reports the appended suffix as after.events[len(before.raw_events):],
    # which is correct only when the parsed stream and the raw rows are the same
    # events in the same order.
    if len(raw_events) != len(events):
        raise ArtifactError(
            f"{source}: the 'events' list has {len(events)} entries but the events table "
            f"{schema.events_table!r} has {len(raw_events)} rows; the two must be the same "
            "event stream in the same order"
        )
    pk = schema.primary_keys[schema.events_table]
    for index, (event, row) in enumerate(zip(events, raw_events)):
        if row[pk] != event.event_id:
            raise ArtifactError(
                f"{source}: events[{index}] is {event.event_id!r} in the 'events' list but "
                f"{row[pk]!r} in the events table; the two representations disagree on order or identity"
            )
    return raw_events


def _resolve_state_hash(raw: dict[str, Any], tables: dict[str, list[Row]], source: str) -> str:
    try:
        computed = sha256_hex(canonical_json({"tables": tables}))
    except CanonicalError as exc:
        raise ArtifactError(f"{source}: cannot canonicalize tables: {exc}") from exc
    supplied = raw.get("state_hash")
    if supplied is None:
        return computed
    if not isinstance(supplied, str):
        raise ArtifactError(f"{source}: 'state_hash' must be a hex string or null, got {supplied!r}")
    if supplied != computed:
        raise ArtifactError(
            f"{source}: state_hash mismatch (embedded {supplied}, recomputed {computed}); "
            "the capture is corrupted or tampered"
        )
    return supplied


def load_snapshot(path: str | Path) -> NormalizedSnapshot:
    """Load and validate one blastradius.snapshot.v1 capture. Fails closed."""
    path = Path(path)
    source = str(path)
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ArtifactError(f"{source}: a capture is a single JSON object, got {type(raw).__name__}")
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, source, "capture")
    if raw.get("format") != FORMAT:
        raise ArtifactError(f"{source}: 'format' must be {FORMAT!r}, got {raw.get('format')!r}")
    schema = _build_schema(raw.get("schema"), source)
    tables = raw.get("tables")
    _verify_tables(schema, tables, source)
    _verify_rows(schema, tables, source)
    events_list = raw.get("events", [])
    events = _parse_events(events_list, source)
    raw_events = _resolve_raw_events(schema, tables, events_list, events, source)
    state_hash = _resolve_state_hash(raw, tables, source)
    return NormalizedSnapshot(
        schema=schema, tables=tables, events=events,
        raw_events=raw_events, state_hash=state_hash, source=source,
    )


def _require_matching_schema(before: NormalizedSnapshot, after: NormalizedSnapshot) -> None:
    """A friendlier refusal than ArtifactPair's post-init.

    The pair's post-init already rejects mismatched primary_keys, but two
    captures can share primary keys and still disagree on columns, event types,
    or which table holds events, which would make the diff read one capture
    through the other's shape. Compare the whole schema and name what differs.
    """
    if before.schema == after.schema:
        return
    fields = ("primary_keys", "columns", "event_types", "non_diffed_tables",
              "events_table", "environment_table", "name")
    differ = [field for field in fields if getattr(before.schema, field) != getattr(after.schema, field)]
    raise ArtifactError(
        f"before and after were captured under different schemas; they disagree on {differ}. "
        "Both states must be graded under one schema or the diff keys rows by a primary key one "
        "side does not have."
    )


class SnapshotAdapter:
    """Loads a before/after pair of blastradius.snapshot.v1 captures.

    Store-neutral: it needs no dependency beyond the standard library and the
    capture's own self-described schema, so a team that can dump state to JSON
    can grade an agent run without writing an adapter. Implements StateAdapter.
    """

    name = ADAPTER_NAME

    def load_pair(
        self,
        before: str | Path,
        after: str | Path,
        before_events: str | Path | None = None,
        after_events: str | Path | None = None,
    ) -> ArtifactPair:
        # Events are embedded in each capture, so an external event log is not a
        # thing this format has. Accepting one silently would let a caller read
        # the pair as cross-checked when nothing was checked; refuse it instead.
        if before_events is not None or after_events is not None:
            raise ArtifactError(
                "snapshot.v1 embeds its events in the capture; external event logs are not "
                "accepted, pass only the before and after capture paths"
            )
        before_snapshot = load_snapshot(before)
        after_snapshot = load_snapshot(after)
        _require_matching_schema(before_snapshot, after_snapshot)
        return ArtifactPair(before=before_snapshot, after=after_snapshot)


def load_pair(before: str | Path, after: str | Path) -> ArtifactPair:
    """Convenience: load a before/after capture pair with the default adapter."""
    return SnapshotAdapter().load_pair(before, after)
