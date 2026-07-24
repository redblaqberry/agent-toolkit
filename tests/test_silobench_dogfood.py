"""The dogfood: grade a real SiloBench capture with BlastRadius, end to end.

Grades real SiloBench snapshot.v1 captures through the SiloBench adapter and the
example AP contract. A correct payment release clears the oracle; the
duplicate-payment defect (a second payment nobody audited) is blocked.

The captures are vendored under tests/fixtures/silobench/ rather than read from a
sibling checkout, because this is the demonstration the README leads with and it
has to run for someone who has only cloned this repository. They were produced by
StateDiff (same author, MIT) from a SiloBench run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blastradius import evaluate, load_scenario
from blastradius.adapters.silobench import SilobenchAdapter

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "examples" / "contracts" / "ap-payment-release.yaml"
FIXTURES = REPO / "tests" / "fixtures" / "silobench"


def _require():
    # a hard failure, not a skip: these ship with the repository, so their
    # absence is a broken checkout rather than an optional extra
    assert (FIXTURES / "baseline" / "payment").is_dir(), (
        f"vendored SiloBench captures are missing from {FIXTURES}"
    )


def _grade(before_dir: str, after_dir: str):
    base = FIXTURES / "baseline" / "payment"
    after = FIXTURES / after_dir
    pair = SilobenchAdapter().load_pair(
        base / "before-snapshot.json",
        after / "after-snapshot.json",
        base / "before-events.jsonl",
        after / "after-events.jsonl",
    )
    return evaluate(load_scenario(CONTRACT), pair)


def test_correct_payment_release_clears_the_oracle():
    _require()
    verdict = _grade("baseline/payment", "baseline/payment")
    assert verdict.ok, verdict.report()
    assert len(verdict.checks) == 11 and not verdict.unexplained


def test_duplicate_payment_is_blocked():
    _require()
    # same before, but the after has a second payment nobody audited: the
    # correlation check and the sweep both catch it.
    verdict = _grade("baseline/payment", "defects/duplicate-payment")
    assert not verdict.ok
    failing = {c.id for c in verdict.checks if c.status == "fail"}
    assert "payment-audited" in failing
    assert "no-second-payment" in failing
    assert verdict.unexplained


def test_a_tampered_snapshot_is_a_fail_closed_error():
    _require()
    # feeding the CLI a corrupted artifact must be an error, not a crash; here we
    # confirm the adapter refuses one of StateDiff's committed corruption defects.
    from blastradius import ArtifactError

    base = FIXTURES / "baseline" / "payment"
    broken = FIXTURES / "defects" / "broken-hash"
    assert broken.is_dir(), f"vendored broken-hash capture is missing from {broken}"
    with pytest.raises(ArtifactError):
        SilobenchAdapter().load_pair(
            base / "before-snapshot.json",
            broken / "after-snapshot.json",
        )
