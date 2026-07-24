"""The ergonomic public API: grade a captured pair, or capture a live store
around an agent run and grade what it changed.

The engine's `evaluate(contract, pair)` is the whole oracle; `grade` is the same
call under the name most people reach for. `capture` is the loop for a live store
(a SQL database): snapshot before, let the agent run, snapshot after, then grade.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .adapter import ArtifactPair, CaptureAdapter, NormalizedSnapshot
from .engine import evaluate
from .models import Verdict
from .scenario import Scenario


def grade(contract: Scenario, pair: ArtifactPair) -> Verdict:
    """Grade a captured before/after pair against a contract. Returns a Verdict
    whose `.ok` is True only on a clean pass; an `error` (unjudgeable run) blocks
    exactly like a `fail`."""
    return evaluate(contract, pair)


@dataclass
class Capture:
    """A before state, and the after state once the captured block finishes."""

    before: NormalizedSnapshot
    after: NormalizedSnapshot | None = None

    @property
    def pair(self) -> ArtifactPair:
        if self.after is None:
            raise RuntimeError("the capture block has not finished; no after state was taken")
        return ArtifactPair(before=self.before, after=self.after)

    def grade(self, contract: Scenario) -> Verdict:
        return evaluate(contract, self.pair)


@contextmanager
def capture(
    adapter: CaptureAdapter,
    *,
    before_label: str = "before",
    after_label: str = "after",
) -> Iterator[Capture]:
    """Capture a live store around a block of work, then grade it.

        with capture(sql_adapter) as run:
            my_agent.do_the_task()
        verdict = run.grade(contract)

    The after state is taken only if the block completes; an exception inside the
    block propagates without capturing, because a run that crashed did not finish
    its side effects and grading a half-applied state would be misleading.
    """
    before = adapter.capture(before_label)
    box = Capture(before=before)
    yield box
    box.after = adapter.capture(after_label)
