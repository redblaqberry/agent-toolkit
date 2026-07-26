"""The demo has to keep separating the two runs, or it is not a demo.

The README makes three claims a reader cannot check without running it: that a
correct run ships, that a defective one is refused, and that the refusal is
traceable to specific instruments. These assert all three, so a component
changing under this repository breaks the build rather than quietly turning the
demo into a story that no longer matches its own output.

    python -m pytest test_loop.py
"""

from __future__ import annotations

import pytest

import run_loop


@pytest.fixture(scope="module")
def clean():
    return run_loop.loop(defective=False)


@pytest.fixture(scope="module")
def defect():
    return run_loop.loop(defective=True)


def test_a_correct_run_passes_every_stage(clean):
    blocked = [s.tool for s in clean if s.blocked]
    assert not blocked, (
        f"the clean run was refused by {blocked}, so the demo now shows a false "
        f"positive and the README's first claim is wrong"
    )


def test_a_defective_run_is_refused(defect):
    assert any(s.blocked for s in defect), (
        "nothing refused the defective run, so the loop is not gating anything"
    )


def test_the_refusal_is_traceable_to_named_instruments(defect):
    """A verdict nobody can attribute is not evidence."""
    refused = {s.tool for s in defect if s.blocked}
    assert {"statediff", "blastradius", "revpack"} <= refused, (
        f"expected all three to refuse it independently, got {sorted(refused)}"
    )


def test_the_contract_stage_is_not_what_catches_it(defect):
    """The contract is about what was agreed, not about what the run did.

    If this ever starts failing on the defective run, the demo has stopped
    separating the two runs for the stated reason and is passing by accident.
    """
    contract = next(s for s in defect if s.tool == "discoveryspec")
    assert not contract.blocked
    assert contract.verdict == "VALID"


def test_every_stage_reports_a_verdict(clean, defect):
    for stages in (clean, defect):
        for stage in stages:
            assert stage.verdict and stage.verdict != "unknown", (
                f"{stage.tool} produced no verdict, so the summary line is "
                f"reporting something it did not measure"
            )


def test_the_readme_claim_about_statediff_still_holds(defect):
    """The README says statediff catches these, rather than claiming it misses them.

    That honesty is load-bearing: it is the difference between a demo and a
    sales pitch. If a future fixture slips past the state oracle, this fails and
    the README paragraph has to be rewritten to match.
    """
    state = next(s for s in defect if s.tool == "statediff")
    assert state.blocked, (
        "statediff no longer catches the defect. The README's 'What this does "
        "not claim' section is now wrong and must be updated before shipping."
    )
