"""pytest plugin for BlastRadius: assert an agent's side effects inside a test.

Registered via the `pytest11` entry point. Provides the `side_effects` fixture,
a context manager that captures the store before the block, lets the agent run,
captures after, grades the change against a contract, and fails the test with the
verdict report if the world is wrong.

    def test_release_pays_once(side_effects):
        adapter = SqlAdapter(engine, events_table="events")
        with side_effects(adapter, contract):
            my_agent.run("release payment for INV-1")
        # the block asserts on exit; a wrong or unjudgeable world fails the test

The fixture works with any CaptureAdapter, so it is not tied to SQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

from .adapter import CaptureAdapter
from .api import Capture, capture
from .scenario import Scenario


@pytest.fixture
def side_effects():
    """Return a context manager `run(adapter, contract)` that grades what the
    block changed and fails the test if the verdict is not a clean pass.

    The verdict is attached to the yielded Capture as `.verdict`, so a test that
    wants to inspect it (rather than only assert pass) can read it after the
    block.
    """

    @contextmanager
    def run(adapter: CaptureAdapter, contract: Scenario, *, assert_ok: bool = True) -> Iterator[Capture]:
        with capture(adapter) as box:
            yield box
        verdict = box.grade(contract)
        # stash it on the box for tests that inspect rather than only assert
        box.verdict = verdict  # type: ignore[attr-defined]
        if assert_ok and not verdict.ok:
            pytest.fail(verdict.report(), pytrace=False)

    return run
