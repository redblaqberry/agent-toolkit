"""Producer parsers. Every case here is a trap verified against real source.

These are the defects that make cross-artifact aggregation hard: a producer that
serializes no verdict, one whose aggregate hides a failure, one whose "passed"
excludes half the check, and a replay adapter that will hand you a trajectory
for a scenario you did not run.
"""

from __future__ import annotations

import json

import pytest

from revpack.errors import InputError
from revpack.lattice import Status
from revpack.parsers import (
    classify_check,
    cross_reference,
    detect_format,
    parse_blast_radius,
    parse_gate_run,
    parse_silobench_verify,
    parse_state_verdict,
)


def gate_run(results):
    return {"run_id": "r", "agent_model": "m", "judge_model": None, "mode": "live", "results": results}


def result(scenario_id, checks, inner_id=None, error=None, judge=None, steps=None):
    return {
        "scenario_id": scenario_id,
        "trajectory": {
            "scenario_id": inner_id if inner_id is not None else scenario_id,
            "model": "claude-opus-4-8",
            "steps": steps if steps is not None else [{"index": 0, "latency_s": 0.1}],
            "final_text": "",
            "error": error,
        },
        "checks": checks,
        "judge": judge,
        "cost_usd": None,
    }


def check(name, passed=True):
    return {"name": name, "passed": passed, "detail": ""}


# ---------------------------------------------------------------------------
# behavioral classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "expected_tool:erp_get_invoice",
        "expected_tools_ordered",
        "forbidden_tool:erp_release_payment",
        "max_calls:docs_search",
        "output_contains:PAY-0001",
        "output_not_contains:IBAN",
        "statediff:one-payment",
    ],
)
def test_behavioral_checks_are_counted(name):
    assert classify_check(name)[0] is True


@pytest.mark.parametrize(
    "name",
    [
        "no_harness_error",
        "no_tool_errors",
        "budget:total_tokens",
        "budget:output_tokens",
        "budget:latency_s",
        "budget:steps",
        "state_checks:missing",
        "state_checks:invalid",
        "state_checks:empty",
    ],
)
def test_non_behavioral_checks_are_not_counted(name):
    # must_not_error defaults True upstream, so no_tool_errors is injected into
    # nearly every scenario. Counting it would make an assertion-density rule
    # pass on a scenario that asserts nothing.
    assert classify_check(name)[0] is False


def test_unknown_bare_name_is_not_counted():
    # Fails closed: not counting yields INCONCLUSIVE, which cannot clear a
    # release, whereas counting could let a junk check satisfy the rule.
    assert classify_check("something_new")[0] is False


# ---------------------------------------------------------------------------
# gate run
# ---------------------------------------------------------------------------


def test_only_harness_checks_is_inconclusive_not_pass():
    reading = parse_gate_run("EV-1", "run.json", gate_run([result("TASK-01", [check("no_tool_errors")])]))
    assert reading.facts["scenarios"]["TASK-01"]["status"] == "INCONCLUSIVE"
    assert reading.facts["scenarios"]["TASK-01"]["behavioral_checks"] == 0
    assert reading.status is Status.INCONCLUSIVE


def test_budget_checks_alone_are_still_no_assertion():
    reading = parse_gate_run(
        "EV-1", "run.json", gate_run([result("TASK-01", [check("no_tool_errors"), check("budget:steps")])])
    )
    assert reading.facts["scenarios"]["TASK-01"]["behavioral_checks"] == 0
    assert reading.status is Status.INCONCLUSIVE


def test_one_behavioral_check_passing_is_a_pass():
    reading = parse_gate_run(
        "EV-1",
        "run.json",
        gate_run([result("TASK-01", [check("expected_tool:erp_get_invoice"), check("no_tool_errors")])]),
    )
    assert reading.facts["scenarios"]["TASK-01"]["status"] == "PASS"
    assert reading.status is Status.PASS


def test_failed_check_is_a_fail():
    reading = parse_gate_run(
        "EV-1", "run.json", gate_run([result("TASK-01", [check("expected_tool:x", passed=False)])])
    )
    assert reading.status is Status.FAIL


def test_failed_judge_criterion_is_a_fail():
    judge = {"model": "j", "criteria": [{"criterion": "polite", "passed": False, "reasoning": ""}]}
    reading = parse_gate_run(
        "EV-1", "run.json", gate_run([result("TASK-01", [check("expected_tool:x")], judge=judge)])
    )
    assert reading.status is Status.FAIL


def test_harness_error_is_inconclusive_not_fail():
    # A harness error collapses the checks array upstream, so the absence of
    # other failures proves nothing about the agent.
    reading = parse_gate_run(
        "EV-1",
        "run.json",
        gate_run([result("TASK-01", [check("no_harness_error", passed=False)], error="boom")]),
    )
    assert reading.status is Status.INCONCLUSIVE
    assert "harness error" in reading.facts["scenarios"]["TASK-01"]["detail"]


def test_fixture_substitution_is_detected():
    # ReplayAdapter loads <fixtures>/<scenario_id>.json and never checks the id
    # inside it, so a fixture filed under the wrong name is accepted upstream.
    reading = parse_gate_run(
        "EV-1",
        "run.json",
        gate_run([result("TASK-10", [check("expected_tool:x")], inner_id="TASK-07")]),
    )
    assert reading.status is Status.INCONCLUSIVE
    assert any("substitution" in p for p in reading.problems)


def test_replay_mode_is_recorded_as_a_derivation():
    payload = gate_run([result("TASK-01", [check("expected_tool:x")])])
    payload["mode"] = "replay"
    reading = parse_gate_run("EV-1", "run.json", payload)
    assert any("historical" in d for d in reading.derivations)


def test_derived_verdict_is_labelled():
    reading = parse_gate_run("EV-1", "run.json", gate_run([result("TASK-01", [check("expected_tool:x")])]))
    assert any("recomputed" in d for d in reading.derivations)


# ---------------------------------------------------------------------------
# statediff
# ---------------------------------------------------------------------------


def statediff(status="pass", provenance=None, artifacts=True, scenario_id="SD-PAY-01", checks=None):
    return {
        "format": "statediff.verdict.v1",
        "scenario_id": scenario_id,
        "title": "t",
        "status": status,
        "error": None if status != "error" else "artifact is corrupted",
        # A real verdict asserts at least one predicate. The default carries a
        # passing check so a `pass` here is a pass on evidence, not the vacuous
        # `pass` over an empty check list that reads as INCONCLUSIVE.
        "checks": [{"id": "one-payment", "status": "pass", "detail": ""}] if checks is None else checks,
        "unexplained": [],
        "artifacts": (
            {
                "schema_version": 1,
                "before_state_hash": "a" * 64,
                "after_state_hash": "b" * 64,
                "before_events": 0,
                "after_events": 1,
            }
            if artifacts
            else None
        ),
        "provenance": provenance,
    }


@pytest.mark.parametrize(
    "raw,expected",
    [("pass", Status.PASS), ("fail", Status.FAIL), ("error", Status.INCONCLUSIVE)],
)
def test_statediff_status_mapping(raw, expected):
    assert parse_state_verdict("EV-1", "v.json", statediff(raw)).status is expected


def test_statediff_unknown_status_is_refused():
    with pytest.raises(InputError, match="status"):
        parse_state_verdict("EV-1", "v.json", statediff("maybe"))


def test_statediff_degraded_scenario_id_is_flagged():
    # On a scenario load failure statediff sets scenario_id to the filename
    # stem, which is not a scenario identifier.
    reading = parse_state_verdict(
        "EV-1", "v.json", statediff("error", provenance=None, scenario_id="payment-release")
    )
    assert any("filename stem" in p for p in reading.problems)


def test_an_unrecognized_check_status_is_not_evidence_of_success():
    """"Not the string 'fail'" is not the same question as "passed".

    Only `fail` was looked for, so a check carrying any other status counted as
    one that succeeded: a new state upstream, a misspelling, or a value somebody
    typed all read as a pass under a top-level `pass`, and the verdict cleared
    the release.
    """
    payload = statediff("pass")
    payload["checks"] = [{"id": "one-payment", "status": "probably-fine", "detail": "d"}]
    reading = parse_state_verdict("EV-1", "v.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert any("does not recognize" in p for p in reading.problems)


def test_the_check_statuses_statediff_actually_emits_are_all_understood():
    """Guards against fixing the hole by refusing real output.

    CheckOutcome upstream is `pass | fail | not_applicable`, and the third is a
    deliberate state rather than a gap: an allowed effect that did not fire
    justified nothing, so it is neither a pass nor a failure. It does not block
    there and it must not block here.
    """
    payload = statediff("pass")
    payload["checks"] = [
        {"id": "a", "status": "pass", "detail": ""},
        {"id": "b", "status": "not_applicable", "detail": ""},
    ]
    reading = parse_state_verdict("EV-1", "v.json", payload)
    assert reading.status is Status.PASS
    assert reading.problems == []


def test_statediff_provenance_is_extracted():
    reading = parse_state_verdict(
        "EV-1",
        "v.json",
        statediff("pass", provenance={"requirements": ["REQ-007"], "turns": ["T23"], "silobench_task": "TASK-10"}),
    )
    assert reading.facts["requirements"] == ["REQ-007"]
    assert reading.facts["silobench_task"] == "TASK-10"


def test_a_statediff_pass_carrying_an_error_is_not_a_pass():
    """The top-level error is a sibling of the status, not display text.

    StateDiff types `error` as `str | None` and fills it only when it could not
    grade the world state, so a `pass` beside a non-null error contradicts
    itself. The error was read only into the detail string, so the verdict stayed
    PASS and cleared the release with the failure printed one field away.
    """
    payload = statediff("pass")
    payload["error"] = "state capture diverged from the run"
    reading = parse_state_verdict("EV-1", "v.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert any("recorded an error has not reported a pass" in p for p in reading.problems)


@pytest.mark.parametrize("value", [False, 0, {}, []])
def test_a_non_string_statediff_error_is_refused(value):
    # `payload.get("error") or ...` truthiness-tested the field, so a falsy
    # non-string error read as no error at all. StateDiff never emits one, so it
    # is malformed rather than silence.
    payload = statediff("pass")
    payload["error"] = value
    with pytest.raises(InputError, match="error"):
        parse_state_verdict("EV-1", "v.json", payload)


# ---------------------------------------------------------------------------
# canary
# ---------------------------------------------------------------------------


def canary(state, affected=None, groups=None, **verdict):
    # The replay counters are part of every verdict the canary emits, and a
    # passing state is only read as a pass once they are present, so the helper
    # carries them exactly as the producer does.
    return {
        "format": "blast-radius.v1",
        "baseline": {"contracts": [], "fingerprint": "a" * 64},
        "candidate": {"contracts": [], "fingerprint": "b" * 64},
        "change_groups": groups or [],
        "affected": affected or [],
        "selection": {},
        "coverage_gaps": [],
        "forbidden_notes": [],
        "replay": {},
        "verdict": {
            "state": state,
            "safe": state == "clean",
            "hard_failures": 0,
            "state_failures": 0,
            **verdict,
        },
    }


@pytest.mark.parametrize(
    "state,expected",
    [
        ("clean", Status.PASS),
        ("breaking", Status.FAIL),
        ("clean-with-gaps", Status.INCONCLUSIVE),
        ("error", Status.INCONCLUSIVE),
    ],
)
def test_canary_state_mapping(state, expected):
    assert parse_blast_radius("EV-1", "r.json", canary(state)).status is expected


def test_canary_affected_map_keeps_the_per_scenario_association():
    # Flattening this would destroy exactly the association the traceability
    # conflict rule compares.
    reading = parse_blast_radius(
        "EV-1",
        "r.json",
        canary("clean", affected=[{"scenario_id": "TASK-10", "requirement_ids": ["REQ-009"]}]),
    )
    assert reading.facts["affected_map"] == {"TASK-10": ["REQ-009"]}


def test_canary_unknown_state_is_refused():
    with pytest.raises(InputError, match="unknown canary verdict state"):
        parse_blast_radius("EV-1", "r.json", canary("probably-fine"))


def test_a_severity_outside_the_ranked_vocabulary_is_refused():
    """A `clean` report must not be able to smuggle a catastrophic change group.

    The contract-change rule counts `breaking`, `risky`, and `info` and nothing
    else, so a group carrying any other severity was counted nowhere: it cleared
    the ceiling by being unrecognized, while the canary's own verdict state
    stayed `clean` because that state is computed from severities it does
    recognize. A severity this build cannot rank cannot be compared against a
    ceiling at all.
    """
    payload = canary("clean", groups=[{"id": "g1", "severity": "catastrophic"}])
    with pytest.raises(InputError, match="outside the"):
        parse_blast_radius("EV-1", "r.json", payload)


def test_a_change_group_with_no_severity_is_refused():
    # Absent is not `info`. An unranked group is one nobody compared.
    with pytest.raises(InputError, match="outside the"):
        parse_blast_radius("EV-1", "r.json", canary("clean", groups=[{"id": "g1"}]))


def test_a_change_group_that_is_not_an_object_is_refused():
    # Filtered out, it would have vanished from the count entirely.
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_blast_radius("EV-1", "r.json", canary("clean", groups=["g1"]))


@pytest.mark.parametrize("severity", ["info", "risky", "breaking"])
def test_the_severities_the_canary_actually_emits_are_all_ranked(severity):
    # Guards against fixing the hole by refusing the real vocabulary too.
    reading = parse_blast_radius(
        "EV-1", "r.json", canary("clean", groups=[{"id": "g1", "severity": severity}])
    )
    assert reading.facts[severity] == 1


def test_a_clean_canary_that_calls_itself_unsafe_is_not_a_pass():
    """The producer's own top-level safety signal has to agree with the state.

    `verdict.get("safe", False)` copied `safe` into the facts and evaluated it
    nowhere, so a report deriving to `clean` while flagging itself unsafe still
    cleared the release on the state field alone.
    """
    reading = parse_blast_radius("EV-1", "r.json", canary("clean", safe=False))
    assert reading.status is Status.INCONCLUSIVE
    assert any("verdict.safe is false" in p for p in reading.problems)


def test_a_clean_canary_that_omits_safe_is_refused():
    # Absent is not "safe". Defaulting to False evaluated nothing; requiring the
    # flag means a clean report cannot clear without the producer stating it.
    payload = canary("clean")
    del payload["verdict"]["safe"]
    with pytest.raises(InputError, match="safe"):
        parse_blast_radius("EV-1", "r.json", payload)


def test_a_clean_canary_with_agreeing_safe_still_passes():
    # Guards the other direction: requiring the flag must not make a genuinely
    # clean report unsatisfiable.
    reading = parse_blast_radius("EV-1", "r.json", canary("clean", safe=True))
    assert reading.status is Status.PASS
    assert reading.problems == []


def test_duplicate_canary_affected_scenarios_are_refused():
    """A repeated affected scenario is a duplicate key spelled inside a list.

    The dict comprehension collapsed these last-wins, so the first entry's
    requirement claim was overwritten by the second and the traceability conflict
    rule compared a claim set one independent opinion short.
    """
    payload = canary(
        "clean",
        affected=[
            {"scenario_id": "TASK-10", "requirement_ids": ["REQ-007"]},
            {"scenario_id": "TASK-10", "requirement_ids": ["REQ-009"]},
        ],
    )
    with pytest.raises(InputError, match="duplicate affected scenario_id"):
        parse_blast_radius("EV-1", "r.json", payload)


# ---------------------------------------------------------------------------
# silobench verify and the golden cross-reference
# ---------------------------------------------------------------------------


def sb(task_id="TASK-10", passed=True, default_run=True, final_hash="c" * 64):
    return {
        "task_id": task_id,
        "title": "t",
        "principal": "ap_approver",
        "reference": "task10",
        "profile": {"schema_version": 1, "docs_outage": False},
        "answer": {},
        "failure": None,
        "verdict": {"passed": passed, "failures": []},
        "final_state_hash": final_hash,
        "is_default_run": default_run,
    }


def golden(tasks):
    from revpack.parsers import parse_environment_golden

    return parse_environment_golden(
        "EV-G",
        "g.json",
        {"format": "silobench-golden.v1", "initial": {"v1": "i" * 64}, "tasks": tasks},
    )


def test_golden_mismatch_turns_a_producer_pass_into_a_fail():
    # verdict.passed covers the checker assertions only; the golden state-hash
    # comparison is computed separately upstream and is not folded into it.
    reading = parse_silobench_verify("EV-1", "v.json", [sb(final_hash="d" * 64)])
    assert reading.status is Status.PASS  # provisional, before cross-reference
    readings = [reading, golden({"TASK-10": "c" * 64})]
    cross_reference(readings)
    assert reading.status is Status.FAIL
    assert any("does not match the committed golden" in p for p in reading.problems)


def test_golden_match_stays_a_pass():
    reading = parse_silobench_verify("EV-1", "v.json", [sb(final_hash="c" * 64)])
    cross_reference([reading, golden({"TASK-10": "c" * 64})])
    assert reading.status is Status.PASS


def test_a_non_default_run_cannot_pass_a_check_it_never_performed():
    # SiloBench deliberately does not compare a non-default run against the
    # default golden, so the comparison genuinely cannot run. That is a reason
    # it was not performed, not a licence to report it as performed: the flag
    # deciding this is written by the producer, so keeping PASS would hand the
    # producer a switch that turns off the one check verdict.passed does not
    # cover, and the mismatching hash below would never be looked at.
    reading = parse_silobench_verify("EV-1", "v.json", [sb(default_run=False, final_hash="d" * 64)])
    cross_reference([reading, golden({"TASK-10": "c" * 64})])
    assert reading.status is Status.INCONCLUSIVE
    assert reading.facts["tasks"]["TASK-10"]["golden"] == "not compared (not a default run)"
    assert any("non-default run" in p for p in reading.problems)


def test_missing_golden_entry_is_inconclusive():
    reading = parse_silobench_verify("EV-1", "v.json", [sb()])
    cross_reference([reading, golden({})])
    assert reading.status is Status.INCONCLUSIVE


def test_absent_golden_evidence_downgrades_a_pass():
    reading = parse_silobench_verify("EV-1", "v.json", [sb()])
    cross_reference([reading])
    assert reading.status is Status.INCONCLUSIVE
    assert any("no environment-golden evidence" in p for p in reading.problems)


def test_failed_checker_is_a_fail_regardless_of_golden():
    reading = parse_silobench_verify("EV-1", "v.json", [sb(passed=False, final_hash="c" * 64)])
    cross_reference([reading, golden({"TASK-10": "c" * 64})])
    assert reading.status is Status.FAIL


def test_a_verdict_claiming_passed_while_listing_failures_is_not_a_pass():
    """A producer contradicting itself has not reported a pass.

    Believing `passed` lets a buggy or doctored aggregate hide the failures
    printed beside it; believing the array overrides the producer on its own
    result. Neither can be established from the artifact, so neither is claimed.
    """
    entry = sb()
    entry["verdict"] = {"passed": True, "failures": [{"check": "balance", "detail": "d"}]}
    reading = parse_silobench_verify("EV-1", "v.json", [entry])
    assert reading.status is Status.INCONCLUSIVE
    assert reading.facts["tasks"]["TASK-10"]["status"] == Status.INCONCLUSIVE.value
    assert any("contradicts itself" in p for p in reading.problems)


def test_a_silobench_pass_recording_a_top_level_failure_is_not_a_pass():
    """SiloBench's top-level `failure` is a sibling of `verdict.passed`.

    The runner forces `passed: false` whenever it sets this FailureInfo, so a
    `passed: true` entry that still carries one contradicts itself. The parser
    read `verdict.passed` and `verdict.failures` but never this field, so a
    runner crash sitting one key away kept its pass. This is the StateDiff error
    sibling in a different producer.
    """
    entry = sb()
    entry["failure"] = {"code": "runner_crashed", "message": "runner crashed", "details": {}}
    reading = parse_silobench_verify("EV-1", "v.json", [entry])
    assert reading.status is Status.INCONCLUSIVE
    assert reading.facts["tasks"]["TASK-10"]["status"] == Status.INCONCLUSIVE.value
    assert any("recorded a failure has not reported a pass" in p for p in reading.problems)


def test_a_silobench_failure_beside_a_failing_verdict_needs_no_special_case():
    # Guards the other direction: an honest failure (passed=false, failure set)
    # is already a FAIL and must stay one, not be lifted to inconclusive.
    entry = sb(passed=False)
    entry["failure"] = {"code": "runner_crashed", "message": "boom", "details": {}}
    reading = parse_silobench_verify("EV-1", "v.json", [entry])
    assert reading.facts["tasks"]["TASK-10"]["status"] == Status.FAIL.value


def test_a_malformed_failure_entry_cannot_empty_the_contradiction():
    # The array was filtered to the entries that were objects, so one malformed
    # entry emptied the list the contradiction check reads and handed back the
    # passing claim it exists to catch.
    entry = sb()
    entry["verdict"] = {"passed": True, "failures": ["balance"]}
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_silobench_verify("EV-1", "v.json", [entry])


@pytest.mark.parametrize("value", ["false", "true", "no", 0, 1, None])
def test_a_non_boolean_docs_outage_is_refused(value):
    """`bool("false")` is True, and that is not a rounding error.

    The outage profile is half of what identifies the environment a SiloBench
    run happened in, and it was read through a truthiness test: a profile
    recording the string "false" bound cleanly to a release declaring an outage
    was in progress. No coercion is safe here, because "false" and "true" are
    equally truthy and either reading silently clears a release.
    """
    entry = sb()
    entry["profile"] = {"schema_version": 1, "docs_outage": value}
    with pytest.raises(InputError, match="docs_outage"):
        parse_silobench_verify("EV-1", "v.json", [entry])


def test_a_profile_that_records_no_outage_state_is_refused():
    # Absent is not False. Defaulting would retire the outage half of the
    # environment binding the moment the producer stopped emitting the field.
    entry = sb()
    entry["profile"] = {"schema_version": 1}
    with pytest.raises(InputError, match="docs_outage"):
        parse_silobench_verify("EV-1", "v.json", [entry])


def test_contradictory_goldens_are_reported_rather_than_resolved_by_order():
    """Two committed goldens cannot both describe one task.

    Only the first `environment-golden` reading was consulted, so a second
    golden disagreeing about a task hash simply never took part: whichever one
    `collect` happened to receive first decided what the release was compared
    against, and the loser bound cleanly beside it.
    """
    reading = parse_silobench_verify("EV-1", "v.json", [sb(final_hash="c" * 64)])
    agrees = golden({"TASK-10": "c" * 64})
    disagrees = golden({"TASK-10": "d" * 64})
    cross_reference([reading, agrees, disagrees])

    assert reading.status is Status.INCONCLUSIVE
    assert reading.facts["tasks"]["TASK-10"]["golden"].startswith("contradictory")
    # Both goldens are marked, because nothing in the artifacts says which one
    # is wrong, and reference data that contradicts reference data is not
    # reference data this release can be checked against.
    assert agrees.status is Status.INCONCLUSIVE
    assert disagrees.status is Status.INCONCLUSIVE


def test_the_golden_answer_does_not_depend_on_collection_order():
    def outcome(order):
        reading = parse_silobench_verify("EV-1", "v.json", [sb(final_hash="c" * 64)])
        cross_reference([reading, *order()])
        return reading.status

    forwards = outcome(lambda: [golden({"TASK-10": "c" * 64}), golden({"TASK-10": "d" * 64})])
    backwards = outcome(lambda: [golden({"TASK-10": "d" * 64}), golden({"TASK-10": "c" * 64})])
    # Previously one order reported a match and the other a mismatch, from the
    # same evidence.
    assert forwards is backwards


def test_a_golden_the_others_agree_with_still_applies():
    # The refusal is scoped to the tasks that actually conflict; a task every
    # golden agrees about is still compared.
    reading = parse_silobench_verify(
        "EV-1", "v.json", [sb(task_id="TASK-10", final_hash="c" * 64)]
    )
    cross_reference(
        [
            reading,
            golden({"TASK-10": "c" * 64, "TASK-12": "e" * 64}),
            golden({"TASK-10": "c" * 64, "TASK-12": "f" * 64}),
        ]
    )
    assert reading.status is Status.PASS
    assert reading.facts["tasks"]["TASK-10"]["golden"] == "match"


# ---------------------------------------------------------------------------
# `passed` is a boolean, not a truthy value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "no", 0.0, [], {}, None, 1])
def test_a_non_boolean_gate_check_result_is_refused(value):
    # `not c.get("passed", False)` is a truthiness test, and every non-empty
    # string is truthy, so `"passed": "false"` counted as a passing check. No
    # coercion is safe: "false" and "true" are equally truthy, so whichever way
    # it is read, one of them silently clears the release.
    payload = gate_run([result("TASK-01", [{"name": "expected_tool:x", "passed": value}])])
    with pytest.raises(InputError, match="passed"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_gate_check_with_no_passed_field_is_refused():
    payload = gate_run([result("TASK-01", [{"name": "expected_tool:x", "detail": ""}])])
    with pytest.raises(InputError, match="no 'passed' field"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_non_boolean_judge_criterion_is_refused():
    judge = {"criteria": [{"criterion": "tone", "passed": "false"}]}
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], judge=judge)])
    with pytest.raises(InputError, match="must be a JSON boolean"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_non_boolean_silobench_verdict_is_refused():
    entry = sb()
    entry["verdict"] = {"passed": "false", "failures": []}
    with pytest.raises(InputError, match="must be a JSON boolean"):
        parse_silobench_verify("EV-1", "v.json", [entry])


@pytest.mark.parametrize("steps", ["oops", {"index": 0, "latency_s": 0.4}, 5])
def test_a_trajectory_whose_steps_are_not_an_array_is_refused(steps):
    """Zero is not an absent measurement, it is the fastest run ever recorded.

    A `steps` field that was not an array contributed nothing and left the total
    at 0.0, and 0.0 satisfies every latency ceiling a blocking SLO rule can
    declare. So the shape that means "this trajectory cannot be read" produced
    the shape that means "this trajectory was faster than the contract required".
    """
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], steps=steps)])
    with pytest.raises(InputError, match="must be a JSON array"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_step_that_is_not_an_object_is_refused():
    # Skipped, it silently shrank the measurement to the steps that parsed.
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], steps=["0.4", 12])])
    with pytest.raises(InputError, match="not a JSON object"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_judge_criterion_that_is_not_an_object_is_refused():
    """A criterion nobody could read is not a criterion nobody failed.

    Criteria were filtered to the ones that were objects, so a criterion
    serialized as a bare string vanished from the set being judged and the
    scenario passed on whatever remained.
    """
    judge = {"model": "j", "criteria": [{"criterion": "tone", "passed": True}, "polite"]}
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], judge=judge)])
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_null_judge_criterion_is_refused():
    judge = {"model": "j", "criteria": [None]}
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], judge=judge)])
    with pytest.raises(InputError, match="no 'passed' field"):
        parse_gate_run("EV-1", "run.json", payload)


def test_an_oracle_check_is_disclosed_as_a_derivation():
    """The disclosure that never fired.

    Names from a merged oracle namespace are counted as behavioral assertions,
    which is a decision to trust another tool's result, and the derivation
    recording that decision was computed by comparing `classify_check`'s reason
    against a string it does not return. It was therefore absent from every pack
    ever built, while the checks were counted regardless.
    """
    reading = parse_gate_run(
        "EV-1", "run.json", gate_run([result("TASK-01", [check("statediff:one-payment")])])
    )
    assert reading.facts["scenarios"]["TASK-01"]["behavioral_checks"] == 1
    assert any("external oracle results" in d for d in reading.derivations)


def test_a_non_numeric_step_latency_is_a_clean_error_not_a_traceback():
    # Summing a raw JSON field aborts with a TypeError on `"latency_s": "0.4"`,
    # and an unhandled exception exits 1, the code reserved for a proven NO_GO.
    payload = gate_run(
        [result("TASK-01", [check("expected_tool:x")], steps=[{"index": 0, "latency_s": "0.4"}])]
    )
    with pytest.raises(InputError, match="not a number"):
        parse_gate_run("EV-1", "run.json", payload)


@pytest.mark.parametrize("value", [False, 0, {}, []])
def test_a_falsy_non_string_trajectory_error_is_refused(value):
    """`elif error:` read false, 0, {}, and [] as no error at all.

    The gate's own `derive_passed` fails a scenario whenever `error is not None`,
    so a malformed error slot is not silence: it is a harness error this build
    cannot render, and it must not clear the branch that catches one.
    """
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], error=value)])
    with pytest.raises(InputError, match="error"):
        parse_gate_run("EV-1", "run.json", payload)


def test_an_empty_string_trajectory_error_is_still_an_error():
    # "" is a value the producer wrote, not silence. `elif error:` read it as no
    # error; the gate treats `error is not None` as a harness error.
    payload = gate_run([result("TASK-01", [check("expected_tool:x")], error="")])
    reading = parse_gate_run("EV-1", "run.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert "harness error" in reading.facts["scenarios"]["TASK-01"]["detail"]


# ---------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------


def test_detect_format_reads_declared_format_fields():
    assert detect_format({"format": "statediff.verdict.v1"}) == "statediff.verdict.v1"
    assert detect_format({"contract_version": "deployment-contract.v1"}) == "deployment-contract.v1"


def test_detect_format_sniffs_artifacts_that_declare_none():
    # agent-eval-gate's run.json carries no format field at all.
    assert detect_format({"results": [], "run_id": "x"}) == "agent-eval-gate.run"
    assert detect_format([{"task_id": "TASK-01", "final_state_hash": "x"}]) == "silobench.verify"
    assert detect_format({"nothing": True}) == "unknown"


def test_absent_is_default_run_is_inconclusive_not_a_silent_skip():
    """A missing flag must not read as "not a default run".

    Collapsing absent into False retires the golden state-hash comparison for
    every task the moment the producer stops emitting the field, and reports
    PASS while doing it.
    """
    from revpack.lattice import Status

    entry = sb(final_hash="d" * 64)
    del entry["is_default_run"]
    reading = parse_silobench_verify("EV-1", "v.json", [entry])
    cross_reference([reading, golden({"TASK-10": "a" * 64})])
    task = reading.facts["tasks"]["TASK-10"]
    assert task["status"] == Status.INCONCLUSIVE.value
    assert "unknown" in task["golden"]
    assert any("is_default_run" in p for p in reading.problems)


def test_declaring_a_non_default_run_makes_the_gap_attributable_not_a_pass():
    """The operator's declaration explains an uncompared final state; it does not
    verify it.

    A non-default run is not compared against the committed golden, so its final
    world state is ungraded either way. Declaring ``expect_non_default_runs`` does
    not turn that missing evidence into a pass: it makes the gap an attributable
    operator claim rather than a silent hole, so the task is INCONCLUSIVE with the
    declaration recorded as the reason, exactly as ``contract_change.declared``
    is treated as an unverified claim rather than evidence.
    """
    from revpack.lattice import Status

    entry = sb(final_hash="d" * 64, default_run=False)
    declared = parse_silobench_verify("EV-1", "v.json", [entry])
    cross_reference([declared, golden({"TASK-10": "a" * 64})], non_default_runs_expected=True)
    task = declared.facts["tasks"]["TASK-10"]
    assert task["status"] == Status.INCONCLUSIVE.value
    assert "declares it expects" in task["golden"]

    # The identical artifact, with nothing declared, is inconclusive too, but as
    # an undeclared gap rather than an attributable one.
    undeclared = parse_silobench_verify("EV-1", "v.json", [entry])
    cross_reference([undeclared, golden({"TASK-10": "a" * 64})])
    assert undeclared.facts["tasks"]["TASK-10"]["status"] == Status.INCONCLUSIVE.value


def test_non_boolean_is_default_run_is_refused():
    entry = sb(final_hash="d" * 64)
    entry["is_default_run"] = "false"
    with pytest.raises(InputError):
        parse_silobench_verify("EV-1", "v.json", [entry])
