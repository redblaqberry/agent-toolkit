"""The generic JSON snapshot adapter: write a before/after pair in the
`blastradius.snapshot.v1` format, grade it, and confirm fail-closed parsing.
Self-contained (tmp files), no sibling checkout needed."""

from __future__ import annotations

import json

import pytest

from blastradius import ArtifactError, evaluate, parse_scenario
from blastradius.adapters.snapshot import SnapshotAdapter


def _snap(orders, events):
    return {
        "format": "blastradius.snapshot.v1",
        "schema": {
            "name": "orders",
            "primary_keys": {"orders": "order_id"},
            "columns": {"orders": ["order_id", "status", "total"]},
            "event_types": ["ORDER_PLACED", "ORDER_SHIPPED"],
        },
        "tables": {"orders": orders},
        "events": events,
    }


def _shipped_event():
    return {"event_id": "E1", "mutation_seq": 1, "ts": "2026-01-01T00:00:00Z", "actor": "agent",
            "system": "ops", "type": "ORDER_SHIPPED", "entity_type": "order", "entity_id": "O1",
            "payload": {"order_id": "O1"}}


CONTRACT = parse_scenario({
    "contract": "blastradius.contract.v1",
    "id": "ORD-01",
    "title": "the order ships, nothing else moves",
    "effects": {
        "expected": [
            {"transition": {"id": "shipped", "table": "orders", "key": {"order_id": "O1"},
                            "field": "status", "from": "placed", "to": "shipped"}},
            {"event_exists": {"id": "ship-event", "type": "ORDER_SHIPPED", "entity_id": "O1", "count": 1}},
        ],
    },
})


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_correct_shipment_passes(tmp_path):
    before = _write(tmp_path, "before.json", _snap([{"order_id": "O1", "status": "placed", "total": 100}], []))
    after = _write(tmp_path, "after.json",
                   _snap([{"order_id": "O1", "status": "shipped", "total": 100}], [_shipped_event()]))
    verdict = evaluate(CONTRACT, SnapshotAdapter().load_pair(before, after))
    assert verdict.ok, verdict.report()


def test_an_unexplained_extra_row_is_blocked(tmp_path):
    before = _write(tmp_path, "before.json", _snap([{"order_id": "O1", "status": "placed", "total": 100}], []))
    after = _write(tmp_path, "after.json", _snap(
        [{"order_id": "O1", "status": "shipped", "total": 100},
         {"order_id": "O2", "status": "placed", "total": 5}],  # nobody asked for O2
        [_shipped_event()]))
    verdict = evaluate(CONTRACT, SnapshotAdapter().load_pair(before, after))
    assert not verdict.ok
    assert "unexplained" in {c.id for c in verdict.checks if c.status == "fail"}


def test_a_hash_mismatch_is_a_fail_closed_error(tmp_path):
    doc = _snap([{"order_id": "O1", "status": "placed", "total": 100}], [])
    doc["state_hash"] = "0" * 64  # a hash that cannot recompute
    path = _write(tmp_path, "bad.json", doc)
    with pytest.raises(ArtifactError):
        SnapshotAdapter().load_pair(path, path)
