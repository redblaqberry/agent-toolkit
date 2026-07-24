"""SQL capture adapter: snapshot a live database before and after an agent runs.

This is the headline adapter. Point it at a SQLAlchemy Engine or a connection
URL, `capture()` the state before the agent acts and again after, and hand the
resulting pair to `evaluate`. The engine, the diff, and the rules never learn it
came from SQL: the adapter reflects the schema, reads every in-scope table into
JSON-safe rows, parses the audit table into EventRecords, and declares a Schema,
exactly the NormalizedSnapshot every other store presents.

Fail-closed, like every adapter: any value it cannot normalize to the canonical
domain, any table without a single-column primary key, any audit row that is not
a valid event, raises ArtifactError, which the engine reports as `error`, never a
pass.

v0.1 limitations, stated honestly rather than hidden:
- Single-column primary keys only. A composite or missing PK is an ArtifactError,
  because the diff keys every row by one column.
- No floating-point columns in diffed tables. Canonical JSON refuses floats to
  avoid guessing a formatting, and a coerced float would not round-trip, so a
  float cell is an ArtifactError. Store money as integer minor units, or as a
  NUMERIC/Decimal column (Decimals are captured: integral ones as int, the rest
  as their exact string). Floats INSIDE an event payload are fine (payloads are
  arbitrary JSON and are not hashed).
- The schema is reflected once, at construction. A run that alters the schema
  (DDL) is out of scope; the adapter grades data changes under a fixed schema.
- Events are read from the database itself, so there is no independent log to
  cross-check against; the pair's cross-checked flags stay False (a claim not
  made). `load_pair`'s `before_events`/`after_events` are accepted but unused.
- In-memory SQLite gives each connection its own empty database. Pass an Engine
  built with a StaticPool if you must use `:memory:`; a file-backed DB just works.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from decimal import Decimal
from typing import Any, Callable, Mapping

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import Engine
from pydantic import ValidationError

from ..adapter import ArtifactError, ArtifactPair, NormalizedSnapshot
from ..canonical import canonical_json, sha256_hex
from ..models import EventRecord, Row
from ..schema import Schema

# The EventRecord fields the adapter sources from the audit table, each defaulting
# to a same-named column. `event_columns` overrides any of these when the store's
# names differ; the rest keep the default.
_EVENT_FIELDS = (
    "event_id", "mutation_seq", "ts", "actor", "system",
    "type", "entity_type", "entity_id", "payload",
)
_EVENT_DEFAULT_COLUMNS = {field: field for field in _EVENT_FIELDS}


def _order_key(value: Any) -> tuple:
    """A total order that keeps integers numeric.

    Rows and events are sorted by this so a capture is a pure function of the
    state, not of row-return order. Sorting by `str(value)` alone would put pk 10
    before pk 2 and break the append-only prefix the moment an id crosses a digit
    boundary, so integers are ordered as numbers; the leading tag keeps unlike
    types from ever being compared to each other.
    """
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    return (3, str(value))


def _json_safe(value: Any, *, allow_float: bool) -> Any:
    """Normalize one captured value into the canonical domain (str/int/None/
    list/dict, plus bool), converting the store types that do not belong to it.

    `allow_float` is False for table cells (they are hashed via canonical JSON,
    which refuses floats) and True for event rows (payloads are arbitrary JSON and
    are compared, not hashed). bool is settled before int on purpose:
    `isinstance(True, int)` holds, and the engine treats a bool distinctly from
    the integer it equals.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if allow_float:
            return value
        raise ArtifactError(
            "v0.1 does not capture floating-point columns: canonical JSON refuses floats "
            "to avoid guessing a formatting, and a coerced float would not round-trip. "
            "Store money as integer minor units or a NUMERIC/Decimal column."
        )
    if isinstance(value, Decimal):
        # Integral decimals become ints; the rest keep their exact digits as text,
        # so no precision is invented and both stay in the canonical domain.
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        # isoformat is stable across processes, unlike the datetime object's repr.
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, memoryview):
        return value.tobytes().hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, allow_float=allow_float) for item in value]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArtifactError(f"captured cell has a non-string JSON key {key!r}")
            out[key] = _json_safe(item, allow_float=allow_float)
        return out
    raise ArtifactError(
        f"cannot normalize a {type(value).__name__} cell to the canonical domain "
        "(str/int/None/list/dict); v0.1 supports int, str, bool, Decimal, datetime, "
        "date/time, bytes, UUID, and JSON list/dict columns"
    )


def _as_text(value: Any) -> str:
    """An event identity column as text; an absent one is the empty default."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any, where: str) -> int:
    """A mutation_seq column as a non-negative int, or an ArtifactError."""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                value = None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ArtifactError(f"{where} holds {value!r}, which is not an integer sequence")
    if value < 0:
        raise ArtifactError(f"{where} is negative ({value}); a mutation sequence cannot be")
    return value


def _as_payload(value: Any) -> dict[str, Any]:
    """An event payload as a dict: a JSON column arrives parsed, a text column is
    parsed here, an absent one is the empty default."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ArtifactError(f"event payload is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ArtifactError(f"event payload must be a JSON object, got {type(parsed).__name__}")
        return parsed
    raise ArtifactError(f"event payload must be a JSON object or its text, got {type(value).__name__}")


class SqlAdapter:
    """Captures a live SQL database as a BlastRadius NormalizedSnapshot.

    Implements the CaptureAdapter protocol: `capture()` snapshots the current
    state in place, and `load_pair()` assembles two captures into an ArtifactPair.

    Example:
        adapter = SqlAdapter("sqlite:///ledger.db", events_table="events")
        before = adapter.capture("before")
        run_agent()
        after = adapter.capture("after")
        verdict = evaluate(contract, adapter.load_pair(before, after))
    """

    def __init__(
        self,
        url_or_engine: str | Engine,
        *,
        tables: list[str] | frozenset[str] | None = None,
        events_table: str | None = None,
        event_columns: Mapping[str, str] | None = None,
        ignore_tables: frozenset[str] = frozenset(),
        name: str = "sql",
    ) -> None:
        self.name = name
        # Accept a live Engine or a URL. A URL-built engine is ours to keep open;
        # an injected one is the caller's, so `dispose()` only touches our own.
        if isinstance(url_or_engine, str):
            self._engine = create_engine(url_or_engine)
            self._owns_engine = True
        elif isinstance(url_or_engine, Engine):
            self._engine = url_or_engine
            self._owns_engine = False
        else:
            raise ArtifactError(
                f"url_or_engine must be a SQLAlchemy URL string or Engine, not "
                f"{type(url_or_engine).__name__}"
            )

        self._events_table = events_table
        self._ignore = frozenset(ignore_tables)
        self._event_columns = dict(_EVENT_DEFAULT_COLUMNS)
        if event_columns:
            unknown = set(event_columns) - set(_EVENT_FIELDS)
            if unknown:
                raise ArtifactError(f"event_columns names unknown event fields: {sorted(unknown)}")
            self._event_columns.update(event_columns)
        # Only columns the caller named explicitly are required to exist; a
        # default-named column that is absent falls back to the field's default.
        self._event_columns_explicit = set(event_columns or ())

        # Reflect once, so both captures declare the same Schema object by
        # construction and ArtifactPair never sees two schemas to reconcile.
        self._metadata = MetaData()
        only = None
        if tables is not None:
            wanted = set(tables) | ({events_table} if events_table else set())
            only = sorted(wanted - self._ignore)
        try:
            self._metadata.reflect(bind=self._engine, only=only)
        except Exception as exc:  # noqa: BLE001 -- any reflection failure is fatal, and fails closed
            raise ArtifactError(f"cannot reflect database schema: {exc}") from exc
        self._schema, self._diff_tables = self._build_schema(tables)

    def _build_schema(self, allowlist) -> tuple[Schema, list[str]]:
        reflected = {name: table for name, table in self._metadata.tables.items()
                     if name not in self._ignore}
        if self._events_table is not None and self._events_table not in reflected:
            raise ArtifactError(f"events_table '{self._events_table}' is not in the database")

        if allowlist is not None:
            missing = [name for name in (set(allowlist) - self._ignore) if name not in reflected]
            if missing:
                raise ArtifactError(f"tables not found in database: {sorted(missing)}")
            in_scope = set(allowlist) - self._ignore
        else:
            in_scope = set(reflected)

        # The events table is captured via the event path, never diffed as a table.
        diff_tables = sorted(in_scope - {self._events_table})
        schema_tables = sorted(set(diff_tables) | ({self._events_table} if self._events_table else set()))

        primary_keys: dict[str, str] = {}
        columns: dict[str, frozenset[str]] = {}
        for name in schema_tables:
            table = reflected[name]
            pk_cols = list(table.primary_key.columns)
            if len(pk_cols) != 1:
                kind = "a composite" if len(pk_cols) > 1 else "no"
                raise ArtifactError(
                    f"table '{name}' has {kind} primary key; the SQL adapter v0.1 supports "
                    "single-column primary keys only"
                )
            primary_keys[name] = pk_cols[0].name
            columns[name] = frozenset(col.name for col in table.columns)

        non_diffed = frozenset({self._events_table}) if self._events_table else frozenset()
        schema = Schema(
            primary_keys=primary_keys,
            columns=columns,
            # Reflection cannot enumerate an audit log's vocabulary, so the schema
            # accepts any event type (None); an event rule naming a type is honored
            # as written rather than rejected against a list we cannot build.
            event_types=None,
            non_diffed_tables=non_diffed,
            events_table=self._events_table,
            environment_table=None,
            name=self.name,
        )
        return schema, diff_tables

    def capture(self, label: str = "") -> NormalizedSnapshot:
        # One connection for the whole snapshot, so every table is read from the
        # same point in time.
        with self._engine.connect() as conn:
            tables = {name: self._select_table(conn, name) for name in self._diff_tables}
            events, raw_events = self._select_events(conn)
        state_hash = sha256_hex(canonical_json({"tables": tables}))
        return NormalizedSnapshot(
            schema=self._schema,
            tables=tables,
            events=events,
            raw_events=raw_events,
            state_hash=state_hash,
            source=f"{self.name}:{label}" if label else self.name,
        )

    def _select_table(self, conn, name: str) -> list[Row]:
        table = self._metadata.tables[name]
        pk = self._schema.primary_keys[name]
        rows = [
            {key: _json_safe(value, allow_float=False) for key, value in mapping.items()}
            for mapping in conn.execute(select(table)).mappings()
        ]
        # Sort by pk so the row list, and the state hash built from it, are a pure
        # function of the state rather than of return order. The diff keys by pk, so
        # this ordering never affects it.
        rows.sort(key=lambda row: _order_key(row[pk]))
        return rows

    def _select_events(self, conn) -> tuple[list[EventRecord], list[Row]]:
        if self._events_table is None:
            return [], []
        table = self._metadata.tables[self._events_table]
        cols = self._schema.columns[self._events_table]
        events_pk = self._schema.primary_keys[self._events_table]
        raw_rows = [
            {key: _json_safe(value, allow_float=True) for key, value in mapping.items()}
            for mapping in conn.execute(select(table)).mappings()
        ]
        # Order by the mutation-sequence column when present, else by the table's
        # own pk; tie-break by pk. An append-only log ordered this way makes the
        # before rows a prefix of the after rows, which is what the invariant reads.
        seq_col = self._event_columns["mutation_seq"]
        order_col = seq_col if seq_col in cols else events_pk
        raw_rows.sort(key=lambda row: (_order_key(row.get(order_col)), _order_key(row.get(events_pk))))
        events = [self._to_event(row, index) for index, row in enumerate(raw_rows)]
        return events, raw_rows

    def _resolve_column(self, field: str, cols: frozenset[str]) -> str | None:
        """The events-table column backing an EventRecord field, or None to use
        the field's default. A column the caller named explicitly must exist; a
        default-named one that is absent is optional."""
        col = self._event_columns[field]
        if col in cols:
            return col
        if field in self._event_columns_explicit:
            raise ArtifactError(
                f"events table '{self._events_table}' has no column '{col}' "
                f"mapped to event field '{field}'"
            )
        return None

    def _to_event(self, row: Row, index: int) -> EventRecord:
        cols = self._schema.columns[self._events_table]
        events_pk = self._schema.primary_keys[self._events_table]

        def value_of(field: str) -> Any:
            col = self._resolve_column(field, cols)
            return row.get(col) if col is not None else None

        # event_id: its column, else the table's own pk (a stable per-row identity).
        raw_id = value_of("event_id")
        if raw_id is None and "event_id" not in self._event_columns_explicit:
            raw_id = row.get(events_pk)
        if raw_id is None:
            raise ArtifactError(f"event row {row!r} has no usable event_id")

        # mutation_seq: its column, else the 0-based position in the ordered log.
        raw_seq = value_of("mutation_seq")
        mutation_seq = index if raw_seq is None else _as_int(
            raw_seq, f"events table '{self._events_table}' mutation_seq"
        )

        try:
            return EventRecord(
                event_id=str(raw_id),
                mutation_seq=mutation_seq,
                ts=_as_text(value_of("ts")),
                actor=_as_text(value_of("actor")),
                system=_as_text(value_of("system")),
                type=_as_text(value_of("type")),
                entity_type=_as_text(value_of("entity_type")),
                entity_id=_as_text(value_of("entity_id")),
                payload=_as_payload(value_of("payload")),
            )
        except ValidationError as exc:
            raise ArtifactError(f"events table row {row!r} is not a valid event: {exc}") from exc

    def load_pair(
        self,
        before: NormalizedSnapshot,
        after: NormalizedSnapshot,
        before_events=None,
        after_events=None,
    ) -> ArtifactPair:
        for label, snap in (("before", before), ("after", after)):
            if not isinstance(snap, NormalizedSnapshot):
                raise ArtifactError(
                    f"{label} must be a NormalizedSnapshot from capture(), not {type(snap).__name__}"
                )
        # v0.1 reads events from the database itself, so there is no independent log
        # to cross-check; `before_events`/`after_events` are accepted for protocol
        # compatibility and ignored, and the pair's flags stay False (never checked
        # is not the same as passed).
        return ArtifactPair(before=before, after=after)

    def capture_around(
        self,
        fn: Callable[[], Any],
        *,
        before_label: str = "before",
        after_label: str = "after",
    ) -> ArtifactPair:
        """Capture, run `fn` (the agent action), capture again, return the pair.
        A convenience for the in-process harness path."""
        before = self.capture(before_label)
        fn()
        after = self.capture(after_label)
        return self.load_pair(before, after)

    def dispose(self) -> None:
        """Dispose the engine, but only when this adapter created it."""
        if self._owns_engine:
            self._engine.dispose()
