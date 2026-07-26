"""The SQL adapter: capture a live SQLite database before and after a mutation
and grade the side effects. Uses a file-backed temp DB (an in-memory SQLite hands
each connection a fresh database, which would defeat before/after capture)."""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text  # noqa: E402

from blastradius import capture, evaluate, parse_scenario  # noqa: E402
from blastradius.adapters.sql import SqlAdapter  # noqa: E402

CONTRACT = parse_scenario({
    "contract": "blastradius.contract.v1",
    "id": "SQL-01",
    "title": "one audited payment, nothing else",
    "effects": {
        "expected": [
            {"count_delta": {"id": "one-payment", "table": "payments", "added": 1,
                             "match": {"invoice_id": "INV1"}}},
            {"event_exists": {"id": "audit", "type": "PAYMENT_RELEASED", "entity_id": "INV1", "count": 1}},
            {"correlated": {"id": "audited", "table": "payments", "match": {"invoice_id": "INV1"},
                            "event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"}, "count": 1}},
        ],
        "forbidden": [{"count_delta": {"id": "no-second", "table": "payments", "added": {"min": 2}}}],
    },
})

_PAYLOAD = '{"payment_id": "P1"}'
_INSERT_PAYMENT = "INSERT INTO payments VALUES ('P1', 'INV1', 100)"
_INSERT_EVENT = (
    "INSERT INTO events VALUES ('E1', 1, '2026-01-01T00:00:00Z', 'agent', 'erp', "
    f"'PAYMENT_RELEASED', 'invoice', 'INV1', '{_PAYLOAD}')"
)


def _make_db(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE payments (payment_id TEXT PRIMARY KEY, invoice_id TEXT, amount INTEGER)"))
        conn.execute(text(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, mutation_seq INTEGER, ts TEXT, "
            "actor TEXT, system TEXT, type TEXT, entity_type TEXT, entity_id TEXT, payload TEXT)"))
    return engine


def test_correct_payment_passes(tmp_path):
    engine = _make_db(tmp_path, "ok.db")
    adapter = SqlAdapter(engine, events_table="events")
    before = adapter.capture("before")
    with engine.begin() as conn:
        conn.execute(text(_INSERT_PAYMENT))
        conn.execute(text(_INSERT_EVENT))
    after = adapter.capture("after")
    verdict = evaluate(CONTRACT, adapter.load_pair(before, after))
    assert verdict.ok, verdict.report()


def test_an_unrequested_row_is_blocked(tmp_path):
    engine = _make_db(tmp_path, "bad.db")
    adapter = SqlAdapter(engine, events_table="events")
    before = adapter.capture("before")
    with engine.begin() as conn:
        conn.execute(text(_INSERT_PAYMENT))
        conn.execute(text(_INSERT_EVENT))
        conn.execute(text("INSERT INTO payments VALUES ('P9', 'INV999', 50)"))  # nobody asked
    after = adapter.capture("after")
    verdict = evaluate(CONTRACT, adapter.load_pair(before, after))
    # the answer (one correct payment for INV1) is right; the extra row is not
    assert not verdict.ok
    assert "unexplained" in {c.id for c in verdict.checks if c.status == "fail"}


def test_capture_context_manager(tmp_path):
    engine = _make_db(tmp_path, "ctx.db")
    adapter = SqlAdapter(engine, events_table="events")
    with capture(adapter) as run:
        with engine.begin() as conn:
            conn.execute(text(_INSERT_PAYMENT))
            conn.execute(text(_INSERT_EVENT))
    assert run.grade(CONTRACT).ok
