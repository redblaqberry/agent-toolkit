"""The `side_effects` pytest fixture, driven by a fake CaptureAdapter so it does
not depend on any real store. The fake returns a before state on its first
capture and an after state on its second, standing in for an agent run."""

from __future__ import annotations

from blastradius import NormalizedSnapshot, Schema, parse_scenario
from blastradius.models import EventRecord

SCHEMA = Schema(
    primary_keys={"payments": "payment_id", "events": "event_id"},
    columns={"payments": frozenset({"payment_id", "invoice_id"}), "events": frozenset({"event_id"})},
    non_diffed_tables=frozenset({"events"}),
    events_table="events",
    name="fake",
)

_E1 = EventRecord(event_id="E1", mutation_seq=1, ts="t", actor="a", system="s",
                  type="PAYMENT_RELEASED", entity_type="invoice", entity_id="INV1",
                  payload={"payment_id": "P1"})


class FakeAdapter:
    """A CaptureAdapter whose two captures model an agent's before and after."""

    name = "fake"

    def __init__(self, after_payments, after_events=()):
        self._states = [([], ()), (after_payments, after_events)]
        self._i = 0

    def capture(self, label: str = "") -> NormalizedSnapshot:
        payments, events = self._states[self._i]
        self._i += 1
        raw = [{"event_id": e.event_id} for e in events]
        return NormalizedSnapshot(schema=SCHEMA, tables={"payments": list(payments), "events": raw},
                                  events=list(events), raw_events=raw, state_hash=f"h-{label}")

    def load_pair(self, *a, **k):  # unused; capture is the live path
        raise NotImplementedError


CONTRACT = parse_scenario({
    "contract": "blastradius.contract.v1",
    "id": "PLUGIN-01",
    "title": "one audited payment",
    "effects": {
        "expected": [
            {"count_delta": {"id": "one", "table": "payments", "added": 1, "match": {"invoice_id": "INV1"}}},
            {"event_exists": {"id": "audit", "type": "PAYMENT_RELEASED", "entity_id": "INV1", "count": 1}},
            {"correlated": {"id": "audited", "table": "payments", "match": {"invoice_id": "INV1"},
                            "event": {"type": "PAYMENT_RELEASED", "payload_key": "payment_id"}, "count": 1}},
        ],
        "forbidden": [{"count_delta": {"id": "no-2nd", "table": "payments", "added": {"min": 2}}}],
    },
})

_OK = [{"payment_id": "P1", "invoice_id": "INV1"}]
_BAD = [{"payment_id": "P1", "invoice_id": "INV1"}, {"payment_id": "P2", "invoice_id": "INV1"}]


def test_fixture_passes_on_a_correct_run(side_effects):
    adapter = FakeAdapter(_OK, (_E1,))
    with side_effects(adapter, CONTRACT) as run:
        pass  # the "agent" is modeled by the adapter's two captures
    assert run.verdict.ok


def test_fixture_surfaces_a_broken_run(side_effects):
    # assert_ok=False so the fixture does not fail this test; we inspect instead.
    adapter = FakeAdapter(_BAD, (_E1,))
    with side_effects(adapter, CONTRACT, assert_ok=False) as run:
        pass
    assert not run.verdict.ok
    assert "unexplained" in {c.id for c in run.verdict.checks if c.status == "fail"}
