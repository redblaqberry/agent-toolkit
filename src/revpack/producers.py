"""Strict validation models for the producer formats revpack consumes.

The parsers in ``parsers.py`` used to hand-check every field they read: is this a
string, is that a boolean, is this list non-empty, is that count an integer. Each
missed field was a hole, and adversarial or malformed artifacts kept finding new
ones one site at a time. This module replaces that with a single gate per format:
every artifact is validated through a strict Pydantic model once, before any
verdict logic runs, so the whole malformed-input class is refused in one place.

Two config choices define the regime:

- ``strict=True``: no coercion. Pydantic will not turn ``"1"`` into ``1``,
  ``"false"`` into ``False``, or ``true`` into ``1``. A producer that emits the
  wrong JSON type for a field this tool reads is refused, not silently corrected.
- ``extra="ignore"``: these are formats other tools own and may extend, so an
  added field is tolerated. Only the fields revpack actually reads are modeled,
  and each is modeled precisely (required-not-defaulted where its absence would
  be evidence loss, non-empty where an empty value names nothing, a closed
  vocabulary where only certain values are interpretable).

Semantic contradictions that still yield a verdict (a ``pass`` sitting beside an
error, or beside a failing check, or beside unexplained changes) are NOT refused
here: they are normalized to INCONCLUSIVE by the parser, because they are a
producer disagreeing with itself rather than a structurally unreadable artifact.
This gate refuses only what cannot be read at all.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import InputError


class StrictProducer(BaseModel):
    """Base: strict types, tolerant of unknown fields."""

    model_config = ConfigDict(strict=True, extra="ignore")


def validate_producer(model: type[BaseModel], payload: Any, path: str, what: str):
    """Validate a raw payload through a strict producer model or raise InputError.

    The one entry point every parser calls first. A ValidationError becomes a
    clean InputError (exit 2), with the offending field paths, rather than a
    pydantic traceback or a per-field check the parser might have forgotten.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise InputError(f"{path}: not a valid {what} document: {problems}") from exc


# ---------------------------------------------------------------------------
# statediff.verdict.v1
# ---------------------------------------------------------------------------


class StateDiffCheck(StrictProducer):
    # A non-empty id (a check that names no predicate asserts nothing) and a
    # string status. The status is a plain non-empty string, not a closed
    # Literal, so a value StateDiff adds later is tolerated and read as
    # INCONCLUSIVE by the parser rather than refused as malformed; only a
    # non-string (a list, a number) is refused here, which is the shape that used
    # to crash a set-membership test with an unhashable-type TypeError.
    id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class StateDiffProvenance(StrictProducer):
    silobench_task: Optional[str] = Field(default=None, min_length=1)
    requirements: list[str] = Field(default_factory=list)
    turns: list[str] = Field(default_factory=list)


class StateDiffArtifacts(StrictProducer):
    schema_version: Optional[int] = None
    before_state_hash: Optional[str] = None
    after_state_hash: Optional[str] = None


class StateDiffVerdict(StrictProducer):
    format: Literal["statediff.verdict.v1"]
    # scenario_id names a scenario this pack traces and keys on, so it is required
    # and non-empty; an error verdict degrades it to the filename stem upstream,
    # which is still non-empty.
    scenario_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "error"]
    error: Optional[str] = None
    checks: list[StateDiffCheck] = Field(default_factory=list)
    # StateDiff's sweep of state changes not justified by any expected or allowed
    # effect. A pass carrying a non-empty one contradicts itself; the parser reads
    # it and refuses to clear on that basis. Element shape is not this tool's to
    # model, so it stays a list of anything.
    unexplained: list[Any] = Field(default_factory=list)
    # artifacts and provenance may be an object or null (an error verdict carries
    # neither); the parser reads a null as empty.
    artifacts: Optional[StateDiffArtifacts] = None
    provenance: Optional[StateDiffProvenance] = None
