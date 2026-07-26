"""The engine, driven through the pluggable adapter interface with a
Python-constructed pair. No store, no files, no network: this is the fast proof
that the generalized diff, the seven rules, the default-deny sweep, and the
fail-closed error path all work off an adapter-supplied Schema.
"""

from __future__ import annotations

import pytest

from blastradius import ArtifactPair, NormalizedSnapshot, Schema, evaluate, parse_scenario
from blastradius.models import EventRecord

SCHEMA = Schema(
    primary_keys={"payments": "payment_id", "events": "event_id"},
    columns={
        "payments": frozenset({"payment_id", "invoice_id", "amount"}),
        "events": frozenset({"event_id"}),
    },
    event_types=None,
    non_diffed_tables=frozenset({"events"}),
    events_table="events",
    environment_table=None,
    name="demo",
)


def _event(eid, etype, entity_id, payload, seq):
    return EventRecord(event_id=eid, mutation_seq=seq, ts="2026-01-01T00:00:00Z",
                       actor="agent", system="erp", type=etype,
                       entity_type="invoice", entity_id=entity_id, payload=payload)


def _snap(payments, events, raw_events, h):
    return NormalizedSnapshot(schema=SCHEMA, tables={"payments": payments, "events": raw_events},
                              events=events, raw_events=raw_events, state_hash=h)


BEFORE = _snap([], [], [], "h_before")
_E1 = _event("E1", "PAYMENT_RELEASED", "INV1", {"payment_id": "P1"}, 1)
AFTER_OK = _snap([{"payment_id": "P1", "invoice_id": "INV1", "amount": 100}],
                 [_E1], [{"event_id": "E1"}], "h_ok")
AFTER_BAD = _snap(
    [{"payment_id": "P1", "invoice_id": "INV1", "amount": 100},
     {"payment_id": "P2", "invoice_id": "INV1", "amount": 100}],
    [_E1], [{"event_id": "E1"}], "h_bad",
)


def _contract(effects):
    return parse_scenario({
        "contract": "blastradius.contract.v1",
        "id": "CORE-01",
        "title": "exactly one audited payment",
        "effects": effects,
    })


ONE_AUDITED_PAYMENT = _contract({
    "expected": [
        {"count_delta": {"id": "one-payment", "table": "payments", "added": 1,
                         "match": {"invoice_id": "INV1"}}},
        {"event_exists": {"id": "audit", "type": "PAYMENT_RELEASED", "entity_id": "INV1", "count": 1}},
        {"correlated": {"id": "audited", "table": "payments", "match": {"invoice_id": "INV1"},
                        "event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"}, "count": 1}},
    ],
    "forbidden": [
        {"count_delta": {"id": "no-second", "table": "payments", "added": {"min": 2}}},
    ],
})


def test_correct_pair_passes():
    verdict = evaluate(ONE_AUDITED_PAYMENT, ArtifactPair(before=BEFORE, after=AFTER_OK))
    assert verdict.ok
    assert not [c.id for c in verdict.checks if c.status == "fail"]


def test_unaudited_extra_row_is_blocked():
    verdict = evaluate(ONE_AUDITED_PAYMENT, ArtifactPair(before=BEFORE, after=AFTER_BAD))
    assert not verdict.ok
    failing = {c.id for c in verdict.checks if c.status == "fail"}
    # the correlation catches the payment no event audits, and the sweep flags it
    assert "audited" in failing
    assert "unexplained" in failing
    assert verdict.unexplained


def test_unknown_table_is_a_fail_closed_error():
    # a contract naming a table the schema does not declare must return an `error`
    # verdict (which blocks a gate), never a pass and never a crash.
    contract = _contract({
        "expected": [{"count_delta": {"id": "ghost", "table": "ghosts", "added": 1}}],
    })
    verdict = evaluate(contract, ArtifactPair(before=BEFORE, after=AFTER_OK))
    assert verdict.status == "error"
    assert not verdict.ok


def test_verdict_api_shapes():
    verdict = evaluate(ONE_AUDITED_PAYMENT, ArtifactPair(before=BEFORE, after=AFTER_BAD))
    text = verdict.report()
    assert "BLOCKED" in text and "CORE-01" in text
    checks = verdict.to_gate_checks()
    # the verdict-level entry is always present and, on a failure, is not passed
    verdict_entry = [c for c in checks if c["name"] == "blast:verdict"]
    assert verdict_entry and verdict_entry[0]["passed"] is False


def test_mismatched_schemas_are_refused():
    other = Schema(primary_keys={"x": "id"}, columns={"x": frozenset({"id"})}, name="other")
    bad_after = NormalizedSnapshot(schema=other, tables={"x": []}, events=[], raw_events=[], state_hash="h")
    with pytest.raises(Exception):
        ArtifactPair(before=BEFORE, after=bad_after)
