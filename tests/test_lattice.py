"""The lattice must be a total order and every reducer total over every input.

The hole this pins: an earlier design declared `reduce` mandatory but left the
ordering undefined, so "one artifact passed and another was inconclusive" had no
defined answer and the safety property could not even be stated.
"""

from __future__ import annotations

import itertools

import pytest

from revpack.errors import PolicyError
from revpack.lattice import Status, rank, reduce_statuses, worst

ALL = [Status.FAIL, Status.INCONCLUSIVE, Status.NOT_APPLICABLE, Status.PASS]


def test_order_is_total_and_strict():
    ranks = [rank(s) for s in ALL]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ALL)


def test_fail_is_the_bottom_and_pass_the_top():
    assert worst(ALL) is Status.FAIL
    assert worst([Status.PASS, Status.NOT_APPLICABLE]) is Status.NOT_APPLICABLE
    assert worst([Status.PASS]) is Status.PASS


def test_empty_reduces_to_inconclusive_not_pass():
    # "No evidence" must never read as "everything passed". This is the single
    # most dangerous default the project could hold.
    assert worst([]) is Status.INCONCLUSIVE
    assert reduce_statuses([], "worst") is Status.INCONCLUSIVE
    assert reduce_statuses([], "all") is Status.INCONCLUSIVE


@pytest.mark.parametrize("pair", list(itertools.product(ALL, repeat=2)))
def test_worst_is_total_over_every_pair(pair):
    result = reduce_statuses([("a", pair[0]), ("b", pair[1])], "worst")
    assert result is min(pair, key=rank)


@pytest.mark.parametrize("pair", list(itertools.product(ALL, repeat=2)))
def test_all_passes_only_when_every_member_passes(pair):
    result = reduce_statuses([("a", pair[0]), ("b", pair[1])], "all")
    if all(s is Status.PASS for s in pair):
        assert result is Status.PASS
    else:
        # Reports the worst problem rather than a generic failure.
        assert result is min(pair, key=rank)


def test_authoritative_selects_the_named_member():
    members = [("EV-0001", Status.FAIL), ("EV-0002", Status.PASS)]
    assert reduce_statuses(members, "authoritative", "EV-0002") is Status.PASS
    assert reduce_statuses(members, "authoritative", "EV-0001") is Status.FAIL


def test_authoritative_without_a_selector_is_an_error():
    with pytest.raises(PolicyError, match="authoritative_evidence_id"):
        reduce_statuses([("EV-0001", Status.PASS)], "authoritative")


def test_authoritative_naming_an_absent_member_is_an_error_not_a_fallback():
    with pytest.raises(PolicyError, match="names no collected evidence"):
        reduce_statuses([("EV-0001", Status.PASS)], "authoritative", "EV-9999")


def test_there_is_no_any_reducer():
    # "At least one artifact passed" is exactly the reasoning that lets a green
    # run from an unrelated release clear this one.
    with pytest.raises(PolicyError, match="unknown reduce mode"):
        reduce_statuses([("EV-0001", Status.PASS)], "any")
