"""Core models BlastRadius produces, independent of any state store.

These are the artifacts the engine reads and writes: the parsed domain event, a
single check outcome, the input fingerprints, and the verdict. Store-specific
formats (a SiloBench snapshot, a SQL capture) live in the adapters, which parse
their own format and hand the engine the normalized view in adapter.py.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Row = dict[str, Any]


class EventRecord(BaseModel):
    """One domain event in the normalized form the rules read. An adapter maps
    its store's audit trail onto these fields; a store without a given field
    supplies a stable default (an empty system, an order-derived mutation_seq)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str
    mutation_seq: int = Field(ge=0)
    ts: str
    actor: str
    system: str
    type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]


class CheckOutcome(BaseModel):
    """One named check in a verdict; the shape a deterministic gate runner
    consumes (name/passed/detail) is derived from this in gate.py.

    `not_applicable` exists because an allowed effect is never required: one
    that matched nothing has justified nothing, and calling that `pass` claims
    a predicate held when it did not. It is not a failure either, so it is its
    own third state rather than a lie in one direction or the other."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    rule: str
    list_name: Literal["expected", "allowed", "forbidden", "invariant", "artifact"] = Field(alias="list")
    status: Literal["pass", "fail", "not_applicable"]
    detail: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactFingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The adapter's schema name (e.g. "silobench.v1", "sql"), so a verdict records
    # which state model it was graded under.
    schema_name: str
    before_state_hash: str
    after_state_hash: str
    # Event counts read from the captured states. They are NOT evidence that
    # anything was cross-checked, which is why the two flags below sit beside
    # them: an independent event log is an optional input, and a capture taken
    # without one must not read as a cross-validated capture.
    before_events: int
    after_events: int
    before_events_cross_checked: bool = False
    after_events_cross_checked: bool = False


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["blastradius.verdict.v1"] = "blastradius.verdict.v1"
    contract_id: str
    title: str | None = None
    status: Literal["pass", "fail", "error"]
    error: str | None = None
    checks: list[CheckOutcome] = Field(default_factory=list)
    unexplained: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: ArtifactFingerprints | None = None
    provenance: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """True only on a clean pass. An error blocks exactly like a fail."""
        return self.status == "pass"

    def to_json(self) -> str:
        import json

        return json.dumps(self.model_dump(by_alias=True), ensure_ascii=False, indent=2)

    def to_gate_checks(self) -> list[dict[str, Any]]:
        """Project to `{name, passed, detail}` records a deterministic release
        gate consumes. The verdict-level entry is always present, so a failing or
        erroring verdict can never read as an all-green list."""
        from .gate import to_gate_checks

        return to_gate_checks(self)

    def report(self) -> str:
        """A short human summary: the GO / BLOCKED decision, the verdict, the
        checks tally with any failing ids, and the count of unexplained changes."""
        decision = "GO" if self.ok else "BLOCKED"
        passed = [c.id for c in self.checks if c.status == "pass"]
        failed = [c.id for c in self.checks if c.status == "fail"]
        lines = [
            f"{decision}: {self.contract_id} ({self.title or 'contract'})",
            f"  verdict:  {self.status.upper()}" + (f" ({self.error})" if self.error else ""),
            f"  checks:   {len(passed)} passed, {len(failed)} failed"
            + (f" ({', '.join(failed)})" if failed else ""),
        ]
        if self.unexplained:
            lines.append(
                f"  unexplained: {len(self.unexplained)} change(s) no expected or allowed effect justifies"
            )
        return "\n".join(lines)
