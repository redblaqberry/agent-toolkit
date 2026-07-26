"""The four-value verdict lattice and its reducers.

Four producers report success in four different vocabularies: uppercase
PASS/FAIL/INCOMPLETE, lowercase pass/fail/error, breaking/clean/clean-with-gaps
plus a boolean, and one producer that serializes no verdict at all. They are
normalized onto:

    FAIL < INCONCLUSIVE < NOT_APPLICABLE < PASS

The order is total and deliberate. FAIL is the bottom because a proven failure
is the most informative bad news. INCONCLUSIVE sits above it because "we do not
know" is weaker than "we know it broke", and below NOT_APPLICABLE because an
inapplicable check is not a gap in knowledge. PASS is the top.

Reduction over several artifacts of one kind is never left to an implementation
default: an earlier draft of this project said reduction was mandatory but left
the order undefined, which meant "one artifact passed and another was
inconclusive" had no answer. Every mode below is total over every input.
"""

from __future__ import annotations

from enum import Enum

from .errors import PolicyError


class Status(str, Enum):
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PASS = "PASS"


_RANK: dict[Status, int] = {
    Status.FAIL: 0,
    Status.INCONCLUSIVE: 1,
    Status.NOT_APPLICABLE: 2,
    Status.PASS: 3,
}

REDUCERS = ("worst", "all", "authoritative")


def rank(status: Status) -> int:
    return _RANK[status]


def worst(statuses: list[Status]) -> Status:
    """Minimum under the lattice order.

    An empty list is INCONCLUSIVE, not PASS. A class with no members has proved
    nothing, and returning PASS for "no evidence" is the single most dangerous
    default this project could hold.
    """
    if not statuses:
        return Status.INCONCLUSIVE
    return min(statuses, key=_RANK.__getitem__)


def reduce_statuses(
    members: list[tuple[str, Status]],
    mode: str,
    authoritative_id: str | None = None,
) -> Status:
    """Reduce ``(evidence_id, status)`` members to one status.

    - ``worst``: the minimum. Any non-PASS member degrades the class.
    - ``all``: PASS only when every member is PASS, otherwise the minimum, so
      the reported status names the worst problem rather than a generic
      failure.
    - ``authoritative``: one named member decides; the rest are informational.
      The selector is required, and a selector naming an absent member is a
      policy error rather than a silent fallback.

    There is no ``any`` mode. "At least one artifact passed" is exactly the
    reasoning that lets a green run from an unrelated release clear a release.
    """
    if mode not in REDUCERS:
        raise PolicyError(f"unknown reduce mode {mode!r}; expected one of {list(REDUCERS)}")

    statuses = [status for _, status in members]

    if mode == "worst":
        return worst(statuses)

    if mode == "all":
        if not statuses:
            return Status.INCONCLUSIVE
        return Status.PASS if all(s is Status.PASS for s in statuses) else worst(statuses)

    if authoritative_id is None:
        raise PolicyError(
            "reduce: authoritative requires an authoritative_evidence_id naming "
            "which artifact decides"
        )
    for evidence_id, status in members:
        if evidence_id == authoritative_id:
            return status
    raise PolicyError(
        f"reduce: authoritative_evidence_id {authoritative_id!r} names no collected "
        f"evidence (present: {sorted(eid for eid, _ in members)})"
    )
