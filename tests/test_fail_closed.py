"""Fail-closed guards: every place a gap must refuse rather than clear a release.

The shape is the same everywhere below. Something cannot be established (a check
name outside the known vocabulary, a contract nobody identified, a rule that
inspected nothing, an artifact tied to no release, a metric that is not a
number), and the convenient answer and the safe answer point in opposite
directions. The convenient answer clears the release.

Each test names the behaviour it protects rather than the shape of the input,
because the input is incidental: the same hole reopens whenever a new branch
treats "could not tell" as "nothing was wrong".
"""

from __future__ import annotations

import json

import pytest

from conftest import CLEAN, CLEAN_EVIDENCE, assert_problem, build_pack, load_yaml, write_yaml
from revpack.errors import InputError, PackError, PolicyError
from revpack.lattice import Status
from revpack.models import Policy, ReleaseDescriptor
from revpack.parsers import (
    classify_check,
    load_json,
    parse_blast_radius,
    parse_contract,
    parse_environment_golden,
    parse_gate_run,
    parse_silobench_verify,
    parse_state_verdict,
)
from revpack.policy import validate_policy


def floor_rules():
    return [
        {"id": "R-007", "kind": "release_binding", "blocking": True},
        {"id": "R-002", "kind": "assertion_density", "blocking": True},
        {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
    ]


def policy(**overrides):
    payload = {
        "policy": "release-policy.v1",
        "id": "p",
        "required_evidence": [
            {"kind": "contract", "min_count": 1, "reduce": "all"},
            {"kind": "gate-run", "min_count": 1, "reduce": "worst"},
        ],
        "rules": floor_rules(),
    }
    payload.update(overrides)
    return Policy.model_validate(payload)


# --- unrecognized check names must not count as assertions -----------------


def test_junk_oracle_namespace_is_not_a_behavioral_assertion():
    # `--state-checks` merges arbitrary names, so counting any colon-bearing
    # name let one line of `{"name": "junk:anything", "passed": true}` satisfy
    # the mandatory assertion-density rule.
    assert classify_check("junk:anything")[0] is False
    assert classify_check("statediff:one-payment")[0] is True


# --- the contract must be identified before it is trusted ------------------


def test_a_json_object_pretending_to_be_a_contract_is_refused():
    fake = {"metadata": {"status": "approved"}, "requirements": [{"id": "REQ-001"}]}
    with pytest.raises(InputError, match="not a deployment-contract.v1"):
        parse_contract("EV-1", "c.json", fake, "a" * 64)


def test_an_empty_signature_block_does_not_count_as_signed():
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved", "approval_signature": {}},
        "requirements": [{"id": "REQ-001"}],
    }
    reading = parse_contract("EV-1", "c.json", payload, "a" * 64)
    assert reading.facts["signature_present"] is False
    assert any("not a well formed attest.v1 block" in p for p in reading.problems)


def test_a_requirement_that_is_not_an_object_is_refused():
    """An approved contract cannot carry a requirement nobody counted.

    Requirements were filtered to the ones that parsed, so a requirement
    serialized as a bare string was dropped before coverage was computed:
    coverage then ran over the survivors, reported every one of them covered,
    and returned GO, while the contract on disk listed more requirements than
    anything in the pack had checked.
    """
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved"},
        "requirements": [{"id": "REQ-001"}, "REQ-002"],
    }
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_contract("EV-1", "c.json", payload, "a" * 64)


@pytest.mark.parametrize("requirement", [{"title": "unnamed"}, {"id": ""}, {"id": 7}])
def test_a_requirement_with_no_usable_id_is_refused(requirement):
    # Nothing can be traced to it and nothing can report it as uncovered, so
    # dropping it would remove it from the count without removing the promise.
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved"},
        "requirements": [{"id": "REQ-001"}, requirement],
    }
    with pytest.raises(InputError, match="no usable id"):
        parse_contract("EV-1", "c.json", payload, "a" * 64)


def test_unknown_contract_status_is_refused():
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "probably-fine"},
        "requirements": [],
    }
    with pytest.raises(InputError, match="not draft or approved"):
        parse_contract("EV-1", "c.json", payload, "a" * 64)


# --- policy floor ----------------------------------------------------------


def test_floor_requires_requirement_coverage():
    # Without it a policy could satisfy every other check while never relating
    # any evidence to any contract requirement.
    with pytest.raises(PolicyError) as exc:
        validate_policy(policy(rules=[r for r in floor_rules() if r["kind"] != "requirement_coverage"]))
    assert_problem(exc, "'requirement_coverage' is always required")


def test_unknown_must_token_is_refused():
    # `signature_verified` was a plausible typo that validated and silently
    # checked nothing.
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            policy(
                required_evidence=[
                    {"kind": "contract", "min_count": 1, "reduce": "all", "must": ["signature_verified"]},
                    {"kind": "gate-run", "min_count": 1, "reduce": "worst"},
                ]
            )
        )
    assert_problem(exc, "unknown must token")


def test_a_must_token_that_nothing_evaluates_is_refused():
    """`must: [approved]` validated and decided nothing.

    It was in the accepted vocabulary and implemented in no branch, so a policy
    author who wrote it was told their requirement was in force while it was
    checked nowhere. A token that is accepted and ignored is worse than one that
    is rejected: the rejection is the only version the author can see.
    """
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            policy(
                required_evidence=[
                    {"kind": "contract", "min_count": 1, "reduce": "all", "must": ["approved"]},
                    {"kind": "gate-run", "min_count": 1, "reduce": "worst"},
                ]
            )
        )
    assert_problem(exc, "unknown must token")


def test_the_must_vocabulary_is_exactly_what_is_implemented():
    # Derived from the table of checks rather than written out beside it, so the
    # two cannot drift apart again and quietly re-open the hole above.
    from revpack.policy import _MUST_CHECKS, MUST_TOKENS

    assert set(MUST_TOKENS) == set(_MUST_CHECKS)


def test_floor_validation_is_order_independent():
    # Keying rules by kind kept only the last, so the same pair passed or failed
    # depending on which came second.
    advisory = {"id": "R-009", "kind": "assertion_density", "blocking": False}
    for rules in ([*floor_rules(), advisory], [advisory, *floor_rules()]):
        with pytest.raises(PolicyError) as exc:
            validate_policy(policy(rules=rules))
        assert_problem(exc, "must be blocking")


# --- vacuous passes --------------------------------------------------------


def test_traceability_entry_naming_no_evidence_does_not_count_as_coverage(tmp_path):
    trace = load_yaml(CLEAN / "traceability.yaml")
    for entry in trace["entries"]:
        entry["silobench_tasks"] = []
        entry["statediff_scenarios"] = []
        entry["gate_scenarios"] = []
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.FAIL
    assert any("names no task or scenario" in f for f in rule.findings)


def test_coverage_cannot_be_established_by_an_id_that_names_no_evidence(tmp_path):
    """A traceability id is a claim, and a claim resolving to nothing is not coverage.

    The rule checked that an entry carried a non-empty id and stopped there, so
    a map pointing every requirement at a scenario nobody ran reported full
    traceability and cleared the release. Naming a task is not evidence that the
    task was executed; the pack either contains an artifact about that id or it
    does not.
    """
    trace = load_yaml(CLEAN / "traceability.yaml")
    for entry in trace["entries"]:
        entry["silobench_tasks"] = []
        entry["statediff_scenarios"] = []
        entry["gate_scenarios"] = ["DOES-NOT-EXIST"]
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.FAIL
    assert any("no traceability entry naming executing evidence" in f for f in rule.findings)
    assert decision.state != "GO"


def test_a_partly_unresolvable_map_is_a_gap_rather_than_full_coverage(tmp_path):
    # One id lands on collected evidence and one does not. The requirement is
    # not uncovered, but part of the evidence declared for it is not in this
    # pack, and reporting that as full coverage would claim what was not shown.
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["entries"][0]["gate_scenarios"] = ["TASK-99"]
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.INCONCLUSIVE
    assert any("TASK-99" in f for f in rule.findings)
    assert decision.state != "GO"


def test_ids_are_resolved_against_the_producer_that_could_have_run_them(tmp_path):
    # A StateDiff scenario id is not a gate scenario id. Unioning every id in
    # the pack into one namespace would let an artifact from one producer
    # satisfy a declaration about another.
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["entries"][0]["gate_scenarios"] = ["SD-PAY-01"]
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.INCONCLUSIVE
    assert any("SD-PAY-01" in f for f in rule.findings)


def test_assertion_density_does_not_pass_after_inspecting_nothing():
    from revpack.models import Rule
    from revpack.policy import _rule_assertion_density

    outcome = _rule_assertion_density(Rule(id="R-002", kind="assertion_density"), readings=[])
    assert outcome.status is Status.INCONCLUSIVE


def test_traceability_conflict_does_not_pass_without_two_sources(tmp_path):
    from revpack.models import Rule, TraceabilityMap
    from revpack.policy import _rule_traceability_conflict

    trace = TraceabilityMap.model_validate(load_yaml(CLEAN / "traceability.yaml"))
    outcome = _rule_traceability_conflict(
        Rule(id="R-008", kind="traceability_conflict"), readings=[], trace=trace
    )
    # Only the map has an opinion, so nothing was cross-checked.
    assert outcome.status is Status.INCONCLUSIVE


# --- release binding -------------------------------------------------------


def test_gate_run_from_a_different_run_id_fails_binding(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    release["gate_run_id"] = "20260101-000000"
    path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=path)
    binding = next(b for b in decision.bindings if b.check == "gate_run_id")
    assert binding.status is Status.FAIL


def test_undeclared_run_id_is_inconclusive_not_pass(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    del release["gate_run_id"]
    path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=path)
    binding = next(b for b in decision.bindings if b.check == "gate_run_id")
    assert binding.status is Status.INCONCLUSIVE
    assert decision.state != "GO"


def test_no_recorded_model_cannot_confirm_the_declared_model():
    # An empty model set made the mismatch check vacuously true: it reported
    # "all scenarios ran on <declared>" having compared nothing.
    from revpack.binding import bind

    payload = {
        "run_id": "20260718-141233",
        "mode": "live",
        "results": [
            {
                "scenario_id": "TASK-01",
                "trajectory": {"scenario_id": "TASK-01", "model": "", "steps": [], "error": None},
                "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                "judge": None,
                "cost_usd": None,
            }
        ],
    }
    reading = parse_gate_run("EV-1", "run.json", payload)
    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    outcome = next(o for o in bind(release, [reading]) if o.check == "agent_model")
    assert outcome.status is Status.INCONCLUSIVE


def test_silobench_run_under_another_profile_fails_binding(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    release["environment"]["schema_version"] = 2
    release["environment"]["initial_state_hash"] = (
        "c6b1bcd35a594ddd20d5fdd98310c764db894ace9914b83ec53b4f0101b2cfa4"
    )
    path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=path)
    binding = next(b for b in decision.bindings if b.check == "environment_profile")
    assert binding.status is Status.FAIL


# --- producer artifact integrity -------------------------------------------


def test_canary_clean_with_replay_failures_is_not_a_pass():
    # The canary picks `clean` from contract-change severity alone, so a
    # targeted replay can fail without moving that state.
    payload = {
        "format": "blast-radius.v1",
        "baseline": {"fingerprint": "a" * 64},
        "candidate": {"fingerprint": "b" * 64},
        "change_groups": [],
        "affected": [],
        "verdict": {"state": "clean", "safe": True, "hard_failures": 2, "state_failures": 0},
    }
    reading = parse_blast_radius("EV-1", "r.json", payload)
    assert reading.status is Status.FAIL
    assert any("replay failure" in p for p in reading.problems)


def test_statediff_verdict_that_contradicts_its_own_checks_is_inconclusive():
    payload = {
        "format": "statediff.verdict.v1",
        "scenario_id": "SD-PAY-01",
        "status": "pass",
        "error": None,
        "checks": [{"id": "one-payment", "status": "fail", "detail": "d"}],
        "unexplained": [],
        "artifacts": None,
        "provenance": None,
    }
    reading = parse_state_verdict("EV-1", "v.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert any("contradicts itself" in p for p in reading.problems)


def test_statediff_without_its_format_marker_is_refused():
    with pytest.raises(InputError, match="statediff.verdict.v1 document"):
        parse_state_verdict("EV-1", "v.json", {"status": "pass"})


def test_duplicate_silobench_task_ids_are_refused():
    # Later entries overwrote earlier ones, so a failing result followed by a
    # passing duplicate silently became a pass.
    entry = {
        "task_id": "TASK-10",
        "verdict": {"passed": False, "failures": []},
        "profile": {"schema_version": 1, "docs_outage": False},
        "final_state_hash": "c" * 64,
        "is_default_run": True,
    }
    passing = {**entry, "verdict": {"passed": True, "failures": []}}
    with pytest.raises(InputError, match="duplicate task_id"):
        parse_silobench_verify("EV-1", "v.json", [entry, passing])


def test_duplicate_gate_scenario_ids_are_refused():
    result = {
        "scenario_id": "TASK-01",
        "trajectory": {"scenario_id": "TASK-01", "model": "m", "steps": [], "error": None},
        "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
        "judge": None,
        "cost_usd": None,
    }
    with pytest.raises(InputError, match="duplicate scenario_id"):
        parse_gate_run("EV-1", "run.json", {"run_id": "r", "mode": "live", "results": [result, result]})


# --- type confusion --------------------------------------------------------


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"contract_version": "deployment-contract.v1", "metadata": "x"}, "must be a JSON object"),
        (
            {"contract_version": "deployment-contract.v1", "metadata": {"project": "p", "status": "approved"}, "requirements": "x"},
            "must be a JSON array",
        ),
    ],
)
def test_wrong_nested_types_raise_input_error_not_attributeerror(payload, match):
    with pytest.raises(InputError, match=match):
        parse_contract("EV-1", "c.json", payload, "a" * 64)


def test_gate_run_with_a_string_trajectory_is_a_clean_error():
    payload = {
        "run_id": "r",
        "mode": "live",
        "results": [{"scenario_id": "TASK-01", "trajectory": "oops", "checks": []}],
    }
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_gate_run("EV-1", "run.json", payload)


# --- envelope integrity ----------------------------------------------------


def test_duplicate_envelope_paths_are_refused(tmp_path):
    # One physical artifact indexed twice would count twice toward min_count.
    pack = tmp_path / "pack"
    build_pack(pack)
    envelopes = json.loads((pack / "envelopes.json").read_text(encoding="utf-8"))
    clone = dict(envelopes["envelopes"][0])
    clone["evidence_id"] = "EV-9999"
    envelopes["envelopes"].append(clone)
    (pack / "envelopes.json").write_text(
        json.dumps(envelopes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    from revpack.pack import compute_decision

    with pytest.raises(PackError, match="indexed more than once"):
        compute_decision(pack)


@pytest.mark.parametrize("spelling", ["{head}/./{tail}", "{head}//{tail}"])
def test_one_file_indexed_under_two_spellings_is_refused(tmp_path, spelling):
    """`evidence/x.json` and `evidence/./x.json` are two strings and one file.

    Path parsing drops a `.` segment and collapses a doubled separator before
    anything looks at the path, so the duplicate check compared two spellings of
    the same bytes and found no repeat. That single file was then read twice by
    every rule and counted twice toward `min_count`, which is enough on its own
    to satisfy a class demanding two independent pieces of evidence.
    """
    from revpack.pack import compute_decision

    pack = tmp_path / "pack"
    build_pack(pack)
    envelopes = json.loads((pack / "envelopes.json").read_text(encoding="utf-8"))
    original = envelopes["envelopes"][0]
    head, _, tail = original["path"].rpartition("/")
    clone = dict(original)
    clone["evidence_id"] = "EV-9999"
    clone["path"] = spelling.format(head=head, tail=tail)
    assert clone["path"] != original["path"]
    envelopes["envelopes"].append(clone)
    (pack / "envelopes.json").write_text(
        json.dumps(envelopes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    with pytest.raises(PackError, match="indexed more than once"):
        compute_decision(pack)


def test_envelope_path_escaping_the_pack_is_refused(tmp_path):
    pack = tmp_path / "pack"
    build_pack(pack)
    envelopes = json.loads((pack / "envelopes.json").read_text(encoding="utf-8"))
    envelopes["envelopes"][0]["path"] = "../outside.json"
    (pack / "envelopes.json").write_text(
        json.dumps(envelopes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    from revpack.pack import compute_decision

    with pytest.raises(PackError, match="must stay inside the pack"):
        compute_decision(pack)


def test_absolute_envelope_path_is_refused(tmp_path):
    pack = tmp_path / "pack"
    build_pack(pack)
    envelopes = json.loads((pack / "envelopes.json").read_text(encoding="utf-8"))
    envelopes["envelopes"][0]["path"] = "C:/Windows/System32/drivers/etc/hosts"
    (pack / "envelopes.json").write_text(
        json.dumps(envelopes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    from revpack.pack import compute_decision

    with pytest.raises(PackError, match="must be relative"):
        compute_decision(pack)


# --- multi-artifact rules must read every artifact -------------------------


def slo_contract(latency_ms=2000, cost_eur=None):
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved"},
        "requirements": [{"id": "REQ-001"}],
        "slo": {"p95_latency_ms": {"value": latency_ms}},
    }
    if cost_eur is not None:
        payload["slo"]["cost_per_invoice_eur"] = {"value": cost_eur}
    return parse_contract("EV-C", "c.json", payload, "a" * 64)


def gate_reading(evidence_id, scenario, latency_s=0.1, cost_usd=None, steps=None):
    payload = {
        "run_id": "r",
        "mode": "live",
        "results": [
            {
                "scenario_id": scenario,
                "trajectory": {
                    "scenario_id": scenario,
                    "model": "m",
                    "steps": (
                        steps if steps is not None else [{"index": 0, "latency_s": latency_s}]
                    ),
                    "error": None,
                },
                "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                "judge": None,
                "cost_usd": cost_usd,
            }
        ],
    }
    return parse_gate_run(evidence_id, "run.json", payload)


def slo_rule():
    from revpack.models import Rule

    return Rule(id="R-003", kind="slo")


def test_slo_examines_every_gate_run_not_just_the_first():
    # `collect` takes a repeated `-e gate-run=`, but this rule reached for the
    # first match with next(), so a second run breaching the agreed ceiling was
    # never compared against it. Every other multi-artifact rule iterates.
    from revpack.policy import _rule_slo

    within = gate_reading("EV-0001", "TASK-01", latency_s=0.5)
    breaching = gate_reading("EV-0002", "TASK-02", latency_s=3.0)
    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), within, breaching], release=None)
    assert outcome.status is Status.FAIL
    assert any("EV-0002" in f for f in outcome.findings)


def test_a_nan_written_by_a_producer_reaches_the_rule_and_is_refused(tmp_path):
    """The NaN path from bytes on disk to a verdict.

    The two tests below build the NaN as a Python float, which proves the rule
    refuses one but not that a producer can deliver one. `json.loads` accepts
    `NaN` even though the JSON grammar has no such literal, so this reads it out
    of a file through the tool's own loader: every comparison against NaN is
    false, so an unrefused NaN reaches the branch that reports a blocking
    latency ceiling as satisfied.
    """
    from revpack.parsers import load_json
    from revpack.policy import _rule_slo

    run = tmp_path / "run.json"
    run.write_text(
        json.dumps(
            {
                "run_id": "r",
                "mode": "live",
                "results": [
                    {
                        "scenario_id": "TASK-01",
                        "trajectory": {
                            "scenario_id": "TASK-01",
                            "model": "m",
                            "steps": [{"index": 0, "latency_s": float("nan")}],
                            "error": None,
                        },
                        "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                        "judge": None,
                        "cost_usd": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert "NaN" in run.read_text(encoding="utf-8"), "the fixture must contain a literal NaN"

    reading = parse_gate_run("EV-0001", str(run), load_json(run))
    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), reading], release=None)
    assert outcome.status is Status.INCONCLUSIVE
    assert any("no usable latency" in f for f in outcome.findings)


def test_a_nan_latency_cannot_satisfy_a_blocking_ceiling():
    # Every comparison against NaN is False, so `NaN > limit` was False and the
    # code fell straight through to the branch reporting the ceiling as met.
    from revpack.policy import _rule_slo

    reading = gate_reading("EV-0001", "TASK-01", latency_s=float("nan"))
    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), reading], release=None)
    assert outcome.status is Status.INCONCLUSIVE
    assert any("no usable latency" in f for f in outcome.findings)


def test_a_negative_latency_cannot_satisfy_a_blocking_ceiling():
    # No run took less than no time, so this is not a fast run, it is a
    # producer reporting something other than the measurement.
    from revpack.policy import _rule_slo

    reading = gate_reading("EV-0001", "TASK-01", latency_s=-5.0)
    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), reading], release=None)
    assert outcome.status is Status.INCONCLUSIVE


def test_a_negative_cost_cannot_satisfy_a_blocking_ceiling(tmp_path):
    from revpack.policy import _rule_slo

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = gate_reading("EV-0001", "TASK-01", cost_usd=-5.0)
    outcome = _rule_slo(
        slo_rule(), readings=[slo_contract(cost_eur=0.08), reading], release=release
    )
    assert outcome.status is Status.INCONCLUSIVE


def test_a_string_metric_is_refused_rather_than_compared():
    from revpack.policy import _rule_slo

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = gate_reading("EV-0001", "TASK-01", cost_usd="0.01")
    outcome = _rule_slo(
        slo_rule(), readings=[slo_contract(cost_eur=0.08), reading], release=release
    )
    assert outcome.status is Status.INCONCLUSIVE


@pytest.mark.parametrize("bad", [0, {}, False, "", []])
def test_a_declared_but_malformed_slo_ceiling_is_unevidenced_not_satisfied(bad):
    """A declared-but-malformed ceiling is not an absent ceiling.

    `_ceiling` used `if not raw`, so `{}`, `0`, `false`, `""`, and `[]` in a
    ceiling slot read as "the contract sets no ceiling here" and a blocking
    latency ceiling reported NOT_APPLICABLE and cleared. Only a missing key is
    absence; a value that cannot be read as a number is unevidenced, which cannot
    clear a release.
    """
    from revpack.policy import _rule_slo

    contract = parse_contract(
        "EV-C",
        "c.json",
        {
            "contract_version": "deployment-contract.v1",
            "metadata": {"project": "p", "status": "approved"},
            "requirements": [{"id": "REQ-001"}],
            "slo": {"p95_latency_ms": bad},
        },
        "a" * 64,
    )
    gate = gate_reading("EV-0001", "TASK-01", latency_s=0.1)
    outcome = _rule_slo(slo_rule(), readings=[contract, gate], release=None)
    assert outcome.status is Status.INCONCLUSIVE
    assert outcome.status is not Status.NOT_APPLICABLE


# --- a blocking rule that declines to run is not a pass --------------------


def test_regression_without_a_baseline_is_inconclusive_not_inapplicable():
    # "No baseline was supplied" is a gap in knowledge, not a question that does
    # not arise. NOT_APPLICABLE said the second, and since _decide_state read
    # only FAIL and INCONCLUSIVE, a blocking regression rule passed silently.
    from revpack.models import Rule
    from revpack.policy import _rule_regression

    outcome = _rule_regression(
        Rule(id="R-006", kind="regression", metric="cost_per_invoice_eur")
    )
    assert outcome.status is Status.INCONCLUSIVE


def test_a_blocking_regression_rule_prevents_go(tmp_path):
    payload = load_yaml(CLEAN / "policy.yaml")
    for rule in payload["rules"]:
        if rule["kind"] == "regression":
            rule["blocking"] = True
    path = write_yaml(tmp_path / "policy.yaml", payload)
    decision = build_pack(tmp_path / "pack", policy=path)
    # The policy asked a question this build cannot answer. Answering it anyway
    # is the only wrong response.
    assert decision.state == "INCOMPLETE"


# --- every evidence kind gets a binding decision ---------------------------


def test_a_prices_artifact_is_not_silently_bound_to_nothing():
    # `prices` had no branch in the dispatch, so it produced no BindingOutcome
    # at all, and release_binding reduces over the outcomes it is handed: an
    # artifact that contributes none cannot lower the result. A stale price
    # table was sealed into the pack as evidence bound to nothing.
    from revpack.binding import bind
    from revpack.parsers import parse_opaque

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = parse_opaque("EV-P", "prices", "prices.json", {"eur_per_usd": 0.92})
    outcomes = bind(release, [reading])
    assert [o.status for o in outcomes] == [Status.INCONCLUSIVE]


def test_every_evidence_kind_produces_a_binding_outcome():
    """No evidence kind can be added without a binding decision.

    This is the check that makes the dispatch exhaustive rather than merely
    complete today: adding a member to EvidenceKind and forgetting the binder
    fails here instead of shipping an artifact bound to nothing.
    """
    from revpack.binding import bind
    from revpack.models import EvidenceKind
    from revpack.parsers import Reading

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    for kind in EvidenceKind.__args__:
        reading = Reading(
            evidence_id="EV-0001", kind=kind, path="p.json", status=Status.NOT_APPLICABLE
        )
        assert bind(release, [reading]), f"evidence kind {kind!r} produced no binding outcome"


# --- authoritative means authoritative -------------------------------------


def test_authoritative_coverage_ignores_the_artifacts_it_demoted():
    """Coverage must come from the artifact the policy said decides.

    `reduce: authoritative` names one artifact as the one that decides and
    demotes the rest to informational. The coverage check unioned scenario ids
    across every reading of the kind, so a demoted artifact could supply the
    coverage that cleared the class the deciding one failed to cover.
    """
    from revpack.models import TraceabilityMap
    from revpack.policy import evaluate_classes

    declared = policy(
        required_evidence=[
            {"kind": "contract", "min_count": 1, "reduce": "all"},
            {
                "kind": "gate-run",
                "min_count": 1,
                "reduce": "authoritative",
                "authoritative_evidence_id": "EV-DECIDES",
                "must_cover": "traceability_scenarios",
            },
        ]
    )
    trace = TraceabilityMap.model_validate(
        {
            "map": "traceability-map.v1",
            "project": "p",
            "contract_sha256": "a" * 64,
            "declared_by": "tester",
            "entries": [
                {
                    "requirement": "REQ-001",
                    "silobench_tasks": ["TASK-01"],
                    "justification": "the scenario the release depends on",
                }
            ],
        }
    )
    decides = gate_reading("EV-DECIDES", "TASK-99")
    demoted = gate_reading("EV-OTHER", "TASK-01")

    outcome = next(
        o for o in evaluate_classes(declared, [decides, demoted], trace) if o.kind == "gate-run"
    )
    assert outcome.uncovered == ["TASK-01"]
    assert outcome.status is Status.INCONCLUSIVE


# --- manifest metadata -----------------------------------------------------


def _resigned_with(tmp_path, keys, mutate):
    """Seal a pack, doctor one manifest entry, and re-sign over the result.

    Re-signed because the point is that a signed manifest can still carry a
    false claim: a signature says who wrote the numbers, never that the numbers
    are true. Editing one without re-signing would only prove the attestation
    works, which is a different test.
    """
    from conftest import read_json, resign_manifest, sealed_pack

    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    mutate(manifest["entries"][0])
    resign_manifest(pack, manifest, private)
    return pack, public


def test_a_signed_manifest_carrying_a_false_size_is_refused(tmp_path, keys):
    from revpack.pack import verify

    def falsify(entry):
        entry["bytes"] = 999999

    pack, public = _resigned_with(tmp_path, keys, falsify)
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "does not match the manifest entry")


def test_a_signed_manifest_carrying_a_false_category_is_refused(tmp_path, keys):
    """Asserted separately from the size, so neither check covers for the other.

    Both are verified in the same loop over the same entries. A single test that
    mutated only one field left the other's verification deletable with the
    suite still green, which is the failure mode a regression test exists to
    make impossible.

    `category` decides how a reader interprets a file: labelling a derived
    document as `evidence` presents this tool's own conclusion as an input it
    was given. The value is a valid one from the enum, so this is a false claim
    rather than a malformed manifest.
    """
    from revpack.pack import verify

    def relabel(entry):
        assert entry["path"] == "decision.json", entry["path"]
        assert entry["category"] == "derived"
        entry["category"] = "evidence"

    pack, public = _resigned_with(tmp_path, keys, relabel)
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "does not match its location")


# --- a file that reads one way and parses another --------------------------


def test_a_duplicate_json_object_key_is_refused(tmp_path):
    """The rewrite nothing downstream can see.

    `json.loads` keeps the last value for a repeated key and drops the first
    without a word. Every check in this project runs over the parsed structure,
    so a verdict written as {"passed": false, "passed": true} is the same object
    as a passing one from the first check onwards: hashing, sealing, and
    recomputation all agree with each other, and the interpretation that clears
    the release is the one that survives. The reviewer who opens the file reads
    the failure; the tool grades the pass.
    """
    path = tmp_path / "verify.json"
    path.write_text(
        '[{"task_id": "TASK-10", "verdict": {"passed": false, "passed": true, '
        '"failures": []}}]',
        encoding="utf-8",
    )
    # The blind spot itself: under last-wins parsing the doctored file and an
    # honestly passing one are indistinguishable, so no hash and no
    # recomputation can name the difference. Only the parser can.
    assert json.loads(path.read_text(encoding="utf-8"))[0]["verdict"]["passed"] is True

    with pytest.raises(InputError, match="duplicate JSON object key 'passed'"):
        load_json(path)


def test_duplicate_keys_are_refused_wherever_evidence_is_read(tmp_path):
    # Refused in the one loader every artifact goes through, not in a parser
    # each new evidence kind would have to remember to call.
    doctored = tmp_path / "verify.json"
    doctored.write_text(
        (CLEAN / "silobench-verify" / "verify.json")
        .read_text(encoding="utf-8")
        .replace('"passed": true', '"passed": false, "passed": true', 1),
        encoding="utf-8",
    )
    evidence = dict(CLEAN_EVIDENCE)
    evidence["silobench-verify"] = doctored
    with pytest.raises(InputError, match="duplicate JSON object key"):
        build_pack(tmp_path / "pack", evidence=evidence)


def test_a_duplicate_key_in_a_pack_input_is_refused(tmp_path):
    """The same rewrite in the documents that set the terms of the decision.

    PyYAML keeps the last value and drops the first exactly as `json.loads`
    does, and the policy is not evidence: it is what the pack is graded against.
    A rule reading `blocking: true` to anyone who opens the file and
    `blocking: false` to the engine turns a blocking check advisory, so a
    release clears carrying the evidence that check exists to refuse, and the
    policy archived beside the verdict still reads `true` to every later reader.
    """
    original = (CLEAN / "policy.yaml").read_text(encoding="utf-8")
    doctored = original.replace(
        "    kind: slo\n    title: Agreed latency and cost ceilings hold\n    blocking: true\n",
        "    kind: slo\n    title: Agreed latency and cost ceilings hold\n"
        "    blocking: true\n    blocking: false\n",
        1,
    )
    assert "blocking: true\n    blocking: false" in doctored, "the fixture shape moved"
    path = tmp_path / "policy.yaml"
    path.write_text(doctored, encoding="utf-8", newline="\n")

    with pytest.raises(InputError, match="duplicate YAML mapping key 'blocking'"):
        build_pack(tmp_path / "pack", policy=path)


# --- absence must not be reported as a measurement -------------------------


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([], id="no steps at all"),
        pytest.param([{"index": 0}], id="a step that records no latency"),
        pytest.param([{"index": 0, "latency_s": 0.1}, {"index": 1}], id="a partly timed run"),
    ],
)
def test_a_trajectory_that_records_no_timing_cannot_satisfy_a_latency_ceiling(steps):
    """Absent timing is not zero timing.

    A missing `latency_s` and an empty `steps` array both totalled to 0.0, which
    is not an absent measurement: it is the fastest run imaginable, and it
    satisfies every ceiling a blocking SLO rule can declare. A partial total is
    the same claim more quietly, because summing the steps that carried a number
    reports a figure smaller than the run took and offers it as the measurement.
    """
    from revpack.policy import _rule_slo

    reading = gate_reading("EV-0001", "TASK-01", steps=steps)
    assert reading.facts["scenarios"]["TASK-01"]["latency_s"] is None

    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), reading], release=None)
    assert outcome.status is Status.INCONCLUSIVE
    assert any("no usable latency" in f for f in outcome.findings)


def test_a_fully_timed_trajectory_is_still_measured():
    # Guards the other direction: refusing every trajectory would close the hole
    # by making the ceiling unenforceable, which is not the same as enforcing it.
    from revpack.policy import _rule_slo

    reading = gate_reading("EV-0001", "TASK-01", steps=[{"latency_s": 0.4}, {"latency_s": 0.5}])
    assert reading.facts["scenarios"]["TASK-01"]["latency_s"] == pytest.approx(0.9)
    outcome = _rule_slo(slo_rule(), readings=[slo_contract(), reading], release=None)
    assert outcome.status is Status.PASS


def canary_payload(state="clean", verdict=None, **extra):
    """A blast-radius report. `verdict` replaces the counters the canary emits."""
    payload = {
        "format": "blast-radius.v1",
        "baseline": {"fingerprint": "a" * 64},
        "candidate": {"fingerprint": "b" * 64},
        "change_groups": [],
        "affected": [],
        "verdict": {
            "state": state,
            "safe": state == "clean",
            **({"hard_failures": 0, "state_failures": 0} if verdict is None else verdict),
        },
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param({}, id="counters absent"),
        pytest.param({"hard_failures": 0}, id="one counter absent"),
        pytest.param({"hard_failures": None, "state_failures": None}, id="counters null"),
        pytest.param({"hard_failures": "0", "state_failures": "0"}, id="counters as strings"),
    ],
)
def test_a_clean_canary_report_that_counts_no_replay_failures_is_refused(verdict):
    """`clean` is a statement about contract severity, not about the replays.

    The counters were read through `or 0`, which answered on the producer's
    behalf whenever the producer did not: a report naming no counters at all
    passed the replay-failure check having established nothing, which is the one
    shape that check exists to catch.
    """
    with pytest.raises(InputError, match="hard_failures|state_failures"):
        parse_blast_radius("EV-1", "r.json", canary_payload(verdict=verdict))


def test_a_report_that_already_fails_is_not_refused_over_a_counter():
    # The counters are demanded where they can change the answer. Under a
    # breaking state this check cannot lift the status, so refusing the artifact
    # would trade a reported failure for an unreadable one.
    reading = parse_blast_radius("EV-1", "r.json", canary_payload(state="breaking"))
    assert reading.status is Status.FAIL


def test_a_task_that_records_no_profile_cannot_borrow_another_task_s_environment():
    """Binding on the strength of the task that answered.

    An empty profile was skipped by a truthiness test and dropped from the
    schema-version set binding compares, so a run of two tasks where only one
    carried a profile bound cleanly for both: the schema and outage checks passed
    on the task that said where it ran, and the one that did not was never
    mentioned anywhere in the pack.
    """
    described = {
        "task_id": "TASK-10",
        "verdict": {"passed": True, "failures": []},
        "profile": {"schema_version": 1, "docs_outage": False},
        "final_state_hash": "c" * 64,
        "is_default_run": True,
    }
    silent = {**described, "task_id": "TASK-12", "profile": {}}
    with pytest.raises(InputError, match="records no profile"):
        parse_silobench_verify("EV-1", "v.json", [described, silent])


def test_a_profile_that_names_no_schema_version_is_refused():
    # The other half of the same hole: `schema_version` was read with `.get` and
    # the None it returned was filtered out of the set binding compares, so a
    # profile naming no schema bound as cleanly as an absent one.
    entry = {
        "task_id": "TASK-10",
        "verdict": {"passed": True, "failures": []},
        "profile": {"docs_outage": False},
        "final_state_hash": "c" * 64,
        "is_default_run": True,
    }
    with pytest.raises(InputError, match="schema_version"):
        parse_silobench_verify("EV-1", "v.json", [entry])


def test_a_scenario_that_names_no_model_cannot_report_the_declared_one():
    """Silence is not agreement.

    Scenarios recording no model were filtered out before the comparison, so the
    set described only the scenarios that answered: a run where one scenario named
    the declared model and another named nothing bound as "all scenarios ran on
    <declared>", a claim about every scenario derived from some of them.
    """
    from revpack.binding import bind

    def result(scenario_id, model):
        return {
            "scenario_id": scenario_id,
            "trajectory": {
                "scenario_id": scenario_id,
                "model": model,
                "steps": [{"index": 0, "latency_s": 0.1}],
                "error": None,
            },
            "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
            "judge": None,
            "cost_usd": None,
        }

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = parse_gate_run(
        "EV-1",
        "run.json",
        {
            "run_id": "r",
            "mode": "live",
            "results": [result("TASK-10", release.agent_model), result("TASK-12", "")],
        },
    )
    outcome = next(o for o in bind(release, [reading]) if o.check == "agent_model")
    assert outcome.status is Status.INCONCLUSIVE
    assert "TASK-12" in outcome.detail


# --- a comparison is only as strong as the shape of what it compares -------


def test_equal_fingerprints_that_are_not_hashes_corroborate_nothing():
    """"x" equals "x", and that was accepted as evidence of no contract change.

    The release's no-change declaration is checkable because the canary
    fingerprints both sides, so identical fingerprints corroborate it. Nothing
    required either side to be a fingerprint, so a report that computed neither
    confirmed whatever the release declared and contributed a passing binding
    check to the decision.
    """
    from revpack.binding import bind

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    assert release.contract_change.declared is False
    reading = parse_blast_radius(
        "EV-1",
        "r.json",
        canary_payload(baseline={"fingerprint": "x"}, candidate={"fingerprint": "x"}),
    )
    outcome = next(o for o in bind(release, [reading]) if o.check == "contract_fingerprints")
    assert outcome.status is Status.INCONCLUSIVE
    assert "not SHA-256" in outcome.detail


def test_a_placeholder_golden_cannot_corroborate_a_placeholder_run():
    # The golden comparison is an equality test between this file and a SiloBench
    # run, and neither side had to look like a hash: a golden recording "x"
    # matched a run reporting "x", and the pack then said that task's final world
    # state matched the committed golden.
    with pytest.raises(InputError, match="not SHA-256 digests"):
        parse_environment_golden(
            "EV-G",
            "g.json",
            {"format": "silobench-golden.v1", "initial": {}, "tasks": {"TASK-10": "x"}},
        )


def test_golden_evidence_that_does_not_identify_itself_is_refused():
    # Every other producer artifact is identified before it is trusted. Without
    # it, any JSON object with a `tasks` key is a committed golden.
    with pytest.raises(InputError, match="not a silobench-golden.v1"):
        parse_environment_golden("EV-G", "g.json", {"tasks": {"TASK-10": "c" * 64}})


def test_two_contract_requirements_sharing_an_id_are_refused():
    """One entry cannot cover two promises.

    Coverage reduces the declared ids to a set and the titles map keeps whichever
    requirement came last, so two distinct requirements sharing an id collapsed
    into one: a single traceability entry reported both as covered, and the
    contract's own count no longer described what was checked.
    """
    payload = {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved"},
        "requirements": [
            {"id": "REQ-001", "title": "payments above the threshold need an approver"},
            {"id": "REQ-001", "title": "the audit log is append only"},
        ],
    }
    with pytest.raises(InputError, match="appear more than once"):
        parse_contract("EV-1", "c.json", payload, "a" * 64)


# --- one document, one answer ----------------------------------------------


def test_the_decision_summary_agrees_with_the_rule_about_what_is_covered(tmp_path):
    """A pack must not report a requirement as covered and uncovered at once.

    The summary counted any requirement a map entry mentioned, while the blocking
    rule resolved the ids that entry names against the evidence actually
    collected. A map pointing every requirement at a scenario nobody ran
    therefore produced a decision listing all of them under covered_requirements
    while the rule beside it reported that none of them has executing evidence.
    Two answers to one question in one signed document is worse than either
    answer alone, because the reader picks, and the summary is the half that gets
    skimmed.
    """
    trace = load_yaml(CLEAN / "traceability.yaml")
    for entry in trace["entries"]:
        entry["silobench_tasks"] = []
        entry["statediff_scenarios"] = []
        entry["gate_scenarios"] = ["DOES-NOT-EXIST"]
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)

    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.FAIL
    uncovered_by_rule = {
        finding.split(":")[0]
        for finding in rule.findings
        if "no traceability entry naming executing evidence" in finding
    }
    assert uncovered_by_rule
    assert decision.traceability.covered_requirements == []
    assert set(decision.traceability.uncovered_requirements) == uncovered_by_rule


# --- an unrecognized run mode is not a live run ----------------------------


def _live_gate(mode):
    return {
        "run_id": "r",
        "mode": mode,
        "results": [
            {
                "scenario_id": "TASK-01",
                "trajectory": {
                    "scenario_id": "TASK-01",
                    "model": "m",
                    "steps": [{"index": 0, "latency_s": 0.1}],
                    "error": None,
                },
                "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                "judge": None,
                "cost_usd": None,
            }
        ],
    }


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("bogus", id="an unknown string"),
        pytest.param(None, id="null"),
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param("Replay", id="a case variant that is not the exact token"),
    ],
)
def test_a_run_mode_outside_the_producer_vocabulary_is_refused(mode):
    """Only the exact string "replay" arms the replay protections.

    The SLO rule refuses to measure a replay's fixture latencies against a
    ceiling, and binding refuses to read a replay's recorded model as evidence
    about this release, and both are reached by ``mode == "replay"``. Every other
    value fell through to the live reading, so a mode this build cannot interpret
    disarmed both protections while looking like a live run. An unrecognized mode
    is an artifact whose provenance cannot be established, not a live run.
    """
    with pytest.raises(InputError, match="outside the"):
        parse_gate_run("EV-1", "run.json", _live_gate(mode))


@pytest.mark.parametrize("mode", ["live", "replay"])
def test_the_run_modes_agent_eval_gate_emits_are_accepted(mode):
    # Guards against fixing the hole by refusing the real vocabulary too.
    reading = parse_gate_run("EV-1", "run.json", _live_gate(mode))
    assert reading.facts["mode"] == mode


def test_a_bogus_run_mode_cannot_be_decided_into_a_go(tmp_path):
    # The reported reproduction end to end: mode "replay" reaches INCOMPLETE while
    # mode "bogus" reached GO, bypassing the historical-latency and model-binding
    # checks. It must now refuse rather than decide.
    doctored = tmp_path / "run.json"
    doctored.write_text(
        (CLEAN / "gate-run" / "run.json")
        .read_text(encoding="utf-8")
        .replace('"mode": "live"', '"mode": "bogus"', 1),
        encoding="utf-8",
    )
    assert '"mode": "bogus"' in doctored.read_text(encoding="utf-8"), "the fixture shape moved"
    evidence = dict(CLEAN_EVIDENCE)
    evidence["gate-run"] = doctored
    with pytest.raises(InputError, match="outside the"):
        build_pack(tmp_path / "pack", evidence=evidence)


def test_an_unreadable_scenario_id_binding_cannot_report_a_clean_bind():
    """The binding sibling of the mode hole.

    The passing branch claims every trajectory carries its own scenario id, and
    it is reached by the absence of the two strings "mismatch" and
    "unverifiable". A third value read from the parser's structured flag would
    let that claim be made about a scenario whose identity nothing established, so
    binding reads the flag as a closed set.
    """
    from revpack.binding import bind
    from revpack.parsers import Reading

    reading = Reading(
        evidence_id="EV-1",
        kind="gate-run",
        path="run.json",
        status=Status.PASS,
        facts={
            "scenarios": {"TASK-01": {"id_binding": "something-new", "model": "m"}},
            "models": ["m"],
            "scenarios_without_model": [],
            "mode": "live",
            "run_id": "",
        },
    )
    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    outcome = next(
        o for o in bind(release, [reading]) if o.check == "scenario_id_matches_trajectory"
    )
    assert outcome.status is Status.INCONCLUSIVE


# --- a state verdict that asserts nothing is not a pass --------------------


def _statediff(checks, status="pass"):
    return {
        "format": "statediff.verdict.v1",
        "scenario_id": "SD-PAY-01",
        "status": status,
        "error": None,
        "checks": checks,
        "unexplained": [],
        "artifacts": None,
        "provenance": None,
    }


@pytest.mark.parametrize(
    "checks",
    [
        pytest.param([], id="no checks at all"),
        pytest.param(
            [{"id": f"c{i}", "status": "not_applicable", "detail": ""} for i in range(4)],
            id="every check not_applicable",
        ),
    ],
)
def test_a_state_verdict_asserting_nothing_is_inconclusive_not_pass(checks):
    """A top-level `pass` over no passing check established no predicate.

    StateDiff's own aggregate is "nothing failed", and an empty check list
    satisfies it, as does a list in which every check is `not_applicable`: an
    allowed effect that never fired justified nothing. Reading either as a pass
    lets a scenario whose world state was never graded stand in for the state
    evidence the pack requires.
    """
    reading = parse_state_verdict("EV-1", "v.json", _statediff(checks))
    assert reading.status is Status.INCONCLUSIVE
    assert any("no state predicate" in p for p in reading.problems)


def test_a_state_verdict_with_one_real_passing_check_still_passes():
    # Guards the other direction: refusing every verdict would close the hole by
    # making the class unsatisfiable, which is not the same as satisfying it.
    reading = parse_state_verdict(
        "EV-1", "v.json", _statediff([{"id": "one-payment", "status": "pass", "detail": ""}])
    )
    assert reading.status is Status.PASS
    assert reading.problems == []


def test_all_not_applicable_state_checks_cannot_reach_a_go(tmp_path):
    """The reported reproduction end to end.

    With the clean fixtures altered so all four checks were N/A, the required
    state-evidence class and the blocking state rule both passed and the policy
    returned GO with no state predicate having passed at all.
    """
    from conftest import read_json, write_json

    sd = read_json(CLEAN / "state-verdict" / "SD-PAY-01.json")
    for check in sd["checks"]:
        check["status"] = "not_applicable"
    path = tmp_path / "SD-PAY-01.json"
    write_json(path, sd)
    evidence = dict(CLEAN_EVIDENCE)
    evidence["state-verdict"] = path
    decision = build_pack(tmp_path / "pack", evidence=evidence)

    assert decision.state != "GO"
    cls = next(c for c in decision.evidence_classes if c.kind == "state-verdict")
    assert cls.status is Status.INCONCLUSIVE
    rule = next(r for r in decision.rules if r.kind == "state_verdict")
    assert rule.status is Status.INCONCLUSIVE


# --- a cost ceiling met by the priced subset alone is not met --------------


def _two_scenario_gate(costs):
    return parse_gate_run(
        "EV-G",
        "run.json",
        {
            "run_id": "r",
            "mode": "live",
            "results": [
                {
                    "scenario_id": f"TASK-{i}",
                    "trajectory": {
                        "scenario_id": f"TASK-{i}",
                        "model": "m",
                        "steps": [{"index": 0, "latency_s": 0.1}],
                        "error": None,
                    },
                    "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                    "judge": None,
                    "cost_usd": cost,
                }
                for i, cost in enumerate(costs)
            ],
        },
    )


def test_a_priced_subset_cannot_satisfy_a_cost_ceiling_for_an_unpriced_run():
    """The sibling of the latency partial-data fix.

    One scenario is priced under the ceiling and another carries no cost at all.
    Skipping the unpriced one measured the priced subset and offered it as the
    run's worst cost, so a blocking cost ceiling was satisfied on the strength of
    the scenarios that answered. An unpriced invocation is not a cheap one, which
    is exactly how the latency rule already treats an untimed scenario.
    """
    from revpack.policy import _rule_slo

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = _two_scenario_gate([0.01, None])
    outcome = _rule_slo(
        slo_rule(), readings=[slo_contract(cost_eur=0.08), reading], release=release
    )
    assert outcome.status is Status.INCONCLUSIVE
    assert any("record no cost" in f for f in outcome.findings)


def test_a_fully_priced_run_within_the_ceiling_still_passes():
    # Guards the other direction, so the fix does not make every cost ceiling
    # unenforceable.
    from revpack.policy import _rule_slo

    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    reading = _two_scenario_gate([0.01, 0.02])
    outcome = _rule_slo(
        slo_rule(), readings=[slo_contract(cost_eur=0.08), reading], release=release
    )
    assert outcome.status is Status.PASS


# --- the envelope index must not state a false byte count ------------------


def test_a_false_envelope_byte_count_is_refused(tmp_path):
    """`envelopes.json` records both a hash and a byte count, and both are read.

    The hash was checked and the byte count was not, so an index that hashed
    correctly while stating a false size could be decided, sealed, and verified.
    `bytes` is a field a reader trusts on its own, so an index whose own numbers
    disagree with the pack it describes is refused.
    """
    from revpack.pack import compute_decision

    pack = tmp_path / "pack"
    build_pack(pack, run_decide=False)
    envelopes = json.loads((pack / "envelopes.json").read_text(encoding="utf-8"))
    # Leave the hash untouched: only the byte count is falsified, so the hash
    # check passes and the size check is the only thing that can catch it.
    envelopes["envelopes"][0]["bytes"] = 999999
    (pack / "envelopes.json").write_text(
        json.dumps(envelopes, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(PackError, match="does not match the envelope index") as exc:
        compute_decision(pack)
    assert_problem(exc, "envelope records 999999 bytes but the file is")


# --- a nested field the parser reads yields a clean InputError, not a crash -


def test_a_non_string_gate_check_name_is_a_clean_error_not_a_traceback():
    # The name reaches `str.startswith` in the classifier, which raised an
    # AttributeError on a non-string: a crash on malformed evidence, reported as
    # an unexpected failure rather than as the unreadable artifact it is.
    payload = _live_gate("live")
    payload["results"][0]["checks"] = [{"name": 123, "passed": True, "detail": ""}]
    with pytest.raises(InputError, match="records a name of"):
        parse_gate_run("EV-1", "run.json", payload)


@pytest.mark.parametrize("field", ["scenario_id", "trajectory"])
def test_a_non_string_gate_scenario_id_is_a_clean_error(field):
    payload = _live_gate("live")
    if field == "scenario_id":
        payload["results"][0]["scenario_id"] = ["TASK-01"]
    else:
        payload["results"][0]["trajectory"]["scenario_id"] = ["TASK-01"]
    with pytest.raises(InputError, match="names no scenario"):
        parse_gate_run("EV-1", "run.json", payload)


def test_a_statediff_requirement_list_that_is_a_string_is_refused():
    # `list("REQ-007")` is seven characters, and the requirement list is one of
    # the three opinions the conflict rule compares. A string silently rewrote it
    # into its characters; it is now refused as not the array it must be.
    payload = _statediff([{"id": "c", "status": "pass", "detail": ""}])
    payload["provenance"] = {"requirements": "REQ-007", "silobench_task": "TASK-10"}
    with pytest.raises(InputError, match="requirements"):
        parse_state_verdict("EV-1", "v.json", payload)


def test_a_statediff_requirement_list_with_a_non_string_member_is_refused():
    # The other half: an array is the right shape, but a member that is not an
    # identifier names nothing to trace or compare.
    payload = _statediff([{"id": "c", "status": "pass", "detail": ""}])
    payload["provenance"] = {"requirements": ["REQ-007", 12], "silobench_task": "TASK-10"}
    with pytest.raises(InputError, match="requirements"):
        parse_state_verdict("EV-1", "v.json", payload)


@pytest.mark.parametrize(
    "field,value,match",
    [
        pytest.param("scenario_id", ["SD"], "scenario_id", id="scenario id as a list"),
        pytest.param(
            "silobench_task", ["TASK-10"], "silobench_task", id="provenance task as a list"
        ),
    ],
)
def test_a_non_string_statediff_identifier_is_a_clean_error(field, value, match):
    payload = _statediff([{"id": "c", "status": "pass", "detail": ""}])
    if field == "scenario_id":
        payload["scenario_id"] = value
    else:
        payload["provenance"] = {"silobench_task": value}
    with pytest.raises(InputError, match=match):
        parse_state_verdict("EV-1", "v.json", payload)


def test_a_non_string_silobench_task_id_is_a_clean_error():
    entry = {
        "task_id": ["TASK-10"],
        "verdict": {"passed": True, "failures": []},
        "profile": {"schema_version": 1, "docs_outage": False},
        "final_state_hash": "c" * 64,
        "is_default_run": True,
    }
    with pytest.raises(InputError, match="names no task"):
        parse_silobench_verify("EV-1", "v.json", [entry])


@pytest.mark.parametrize(
    "affected,match",
    [
        pytest.param(["TASK-01"], "must be a JSON object", id="an entry that is not an object"),
        pytest.param([{"requirement_ids": ["REQ-001"]}], "name no scenario", id="an entry naming no scenario"),
        pytest.param(
            [{"scenario_id": "TASK-01", "requirement_ids": "REQ-001"}],
            "must be a JSON array",
            id="requirement_ids as a string",
        ),
        pytest.param(
            [{"scenario_id": "TASK-01", "requirement_ids": ["REQ-001", 9]}],
            "are not strings",
            id="requirement_ids with a non-string member",
        ),
    ],
)
def test_a_malformed_canary_affected_entry_is_refused(affected, match):
    """The canary's consumer map is the third opinion the conflict rule compares.

    Filtering out an unreadable entry removed that opinion, so two producers
    stopped disagreeing without either changing its mind. Each malformed shape is
    refused rather than dropped.
    """
    from conftest import CLEAN as _CLEAN  # noqa: F401

    payload = {
        "format": "blast-radius.v1",
        "baseline": {"fingerprint": "a" * 64},
        "candidate": {"fingerprint": "b" * 64},
        "change_groups": [],
        "affected": affected,
        "verdict": {"state": "clean", "safe": True, "hard_failures": 0, "state_failures": 0},
    }
    with pytest.raises(InputError, match=match):
        parse_blast_radius("EV-1", "r.json", payload)


@pytest.mark.parametrize("side", ["baseline", "candidate"])
def test_a_canary_fingerprint_container_that_is_a_string_is_a_clean_error(side):
    # `(payload.get(side) or {}).get(...)` raised an AttributeError when the side
    # was serialized as a string rather than an object.
    payload = {
        "format": "blast-radius.v1",
        "baseline": {"fingerprint": "a" * 64},
        "candidate": {"fingerprint": "b" * 64},
        "change_groups": [],
        "affected": [],
        "verdict": {"state": "clean", "safe": True, "hard_failures": 0, "state_failures": 0},
    }
    payload[side] = "not-an-object"
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_blast_radius("EV-1", "r.json", payload)


# ---------------------------------------------------------------------------
# cross-model review follow-ups: producer/model fail-closed gaps
# ---------------------------------------------------------------------------

from revpack.models import Environment  # noqa: E402
from revpack.policy import _ceiling  # noqa: E402


def _release_payload(**over):
    payload = {
        "release": "release.v1", "id": "r", "contract_sha256": "a" * 64, "agent_model": "m",
        "environment": {"seed": 4711, "schema_version": 1, "docs_outage": False,
                        "initial_state_hash": "b" * 64},
        "contract_change": {"declared": False},
    }
    payload.update(over)
    return payload


def test_environment_identity_fields_are_type_strict():
    """seed/schema_version/docs_outage are release identity, so a string like
    "1" or "false" must not be coerced into a number or bool that then binds
    cleanly against a differently-typed environment."""
    for bad in ({"seed": "1"}, {"schema_version": "1"}, {"docs_outage": "false"}):
        env = {"seed": 4711, "schema_version": 1, "docs_outage": False,
               "initial_state_hash": "b" * 64, **bad}
        with pytest.raises(Exception):
            Environment.model_validate(env)


def test_non_finite_exchange_rate_is_refused():
    """`0 * inf` is NaN and `NaN > ceiling` is false, so an infinite rate would
    let a blocking cost ceiling report PASS while rendering 'nan within ceiling'."""
    with pytest.raises(Exception):
        ReleaseDescriptor.model_validate(_release_payload(eur_per_usd=float("inf")))
    with pytest.raises(Exception):
        ReleaseDescriptor.model_validate(_release_payload(eur_per_usd=float("nan")))


def test_null_slo_ceiling_is_declared_not_absent():
    """`p95_latency_ms: null` is a declared-but-unusable ceiling, not silence;
    reading it as absent lets a blocking SLO pass on cost alone."""
    value, declared = _ceiling({"p95_latency_ms": None}, "p95_latency_ms")
    assert declared is True
    assert value is None


def test_canary_clean_report_with_a_breaking_group_is_not_pass():
    """The parser trusted verdict.state and a couple of counters; a forged clean,
    safe report carrying an actual breaking change group must not read PASS."""
    r = parse_blast_radius("EV", "b.json", {
        "format": "blast-radius.v1",
        "verdict": {"state": "clean", "safe": True, "hard_failures": 0, "state_failures": 0},
        "change_groups": [{"severity": "breaking"}],
        "affected": [],
    })
    assert r.status is not Status.PASS


def test_a_behavioral_check_name_with_no_identifier_is_not_counted():
    """`expected_tool:` with nothing after the colon names no tool; counting it
    lets a scenario satisfy assertion density while asserting nothing."""
    assert classify_check("expected_tool:")[0] is False
    assert classify_check("forbidden_tool:")[0] is False
    assert classify_check("output_contains:")[0] is False


def test_a_statediff_check_with_no_id_is_refused():
    """A check that reports a status but names no predicate establishes nothing,
    so a top-level pass over such checks is not evidence the state was graded. It
    is refused outright, which is stronger than reading it as merely non-passing."""
    with pytest.raises(InputError):
        parse_state_verdict("EV", "x.json", {
            "format": "statediff.verdict.v1", "status": "pass", "scenario_id": "SD-01",
            "checks": [{"status": "pass"}]})


def test_all_null_approval_signature_is_not_present():
    """An approval_signature whose six keys are all null is not a signature;
    reporting it present would satisfy a `must: [signature_present]` requirement."""
    r = parse_contract("EV", "c.json", {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved",
                     "approval_signature": {k: None for k in
                        ("version", "kind", "algorithm", "public_key_fingerprint",
                         "digest_sha256", "signature")}},
        "requirements": [{"id": "REQ-001", "title": "t"}]}, "abc")
    assert r.facts.get("signature_present") is False


def test_a_nonscalar_check_status_is_a_clean_input_error():
    """A check `status: []` is unhashable; the set logic used to raise TypeError
    and crash to an exit-1 traceback instead of a clean exit-2 InputError."""
    with pytest.raises(InputError):
        parse_state_verdict("EV", "x.json", {
            "format": "statediff.verdict.v1", "status": "pass",
            "checks": [{"id": "c", "status": []}]})


def test_empty_producer_ids_are_refused():
    """An empty task or scenario id names nothing this pack can trace; it used to
    key into a dict and satisfy a golden/coverage lookup on the empty key."""
    with pytest.raises(InputError):
        parse_silobench_verify("EV", "s.json", [
            {"task_id": "", "verdict": {"passed": True},
             "profile": {"schema_version": 1, "docs_outage": False},
             "final_state_hash": "a" * 64}])
    with pytest.raises(InputError):
        parse_state_verdict("EV", "x.json", {
            "format": "statediff.verdict.v1", "status": "pass", "scenario_id": "",
            "checks": [{"id": "c", "status": "pass"}]})


def test_seal_refuses_a_signing_key_stored_inside_the_pack(tmp_path):
    """_pack_files indexes every regular file, so a private key inside the pack
    would be hashed, signed, and shipped with the dossier. Refuse before reading."""
    from revpack.pack import seal
    pack = tmp_path / "pack"
    pack.mkdir()
    key_inside = pack / "sign.pem"
    key_inside.write_text("dummy", encoding="utf-8")
    with pytest.raises(PackError, match="inside the pack"):
        seal(pack, str(key_inside))


def test_a_collected_failing_reading_blocks_even_when_no_class_required_it():
    """A policy requiring only contract and gate-run left a collected, bound
    state-verdict that normalized to FAIL out of every class and rule, so the
    proven failure disappeared and the release cleared. Collected evidence of a
    failure blocks whether or not the policy asked for it."""
    from revpack.pack import _decide_state
    from revpack.parsers import Reading
    failing = Reading(evidence_id="EV", kind="state-verdict", path="x", status=Status.FAIL)
    assert _decide_state([], [], [failing]) == "NO_GO"
    passing = Reading(evidence_id="EV", kind="state-verdict", path="x", status=Status.PASS)
    assert _decide_state([], [], [passing]) == "GO"


def test_a_no_change_release_may_not_carry_change_fingerprints():
    """declared:false binds against equal report fingerprints and never reads the
    declared ones, so a no-change release naming two different contracts was
    accepted with the contradiction ignored. A no-change release names no hashes."""
    from revpack.models import ContractChange
    with pytest.raises(Exception):
        ContractChange.model_validate({"declared": False, "baseline_fingerprint": "a" * 64,
                                       "candidate_fingerprint": "b" * 64})
    # the well-formed no-change and declared-change shapes still validate
    assert ContractChange.model_validate({"declared": False}).declared is False
    assert ContractChange.model_validate({"declared": True, "baseline_fingerprint": "a" * 64,
                                          "candidate_fingerprint": "b" * 64}).declared is True


def test_a_nonscalar_canary_severity_is_a_clean_input_error():
    """A change-group `severity: []` is unhashable; the membership test used to
    raise TypeError and crash rather than refusing the artifact cleanly."""
    with pytest.raises(InputError):
        parse_blast_radius("EV", "b.json", {
            "format": "blast-radius.v1",
            "verdict": {"state": "breaking", "safe": False},
            "change_groups": [{"severity": []}], "affected": []})


def test_a_pass_verdict_with_unexplained_changes_is_inconclusive():
    """StateDiff's unexplained sweep records changes no expected or allowed effect
    justifies. A pass beside a non-empty unexplained set contradicts itself; that
    array used to be dead evidence the parser never read."""
    payload = _statediff([{"id": "unexplained", "rule": "unexplained_sweep",
                           "status": "pass", "detail": "d"}])
    payload["unexplained"] = [{"table": "invoices", "change": "unexpected write"}]
    reading = parse_state_verdict("EV-1", "v.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert any("unexplained" in p for p in reading.problems)


def test_duplicate_behavioral_check_names_count_once():
    """Two identical expected_tool: checks are one assertion asserted twice; they
    used to inflate behavioral density and satisfy a min of 2 with one tool."""
    from revpack.parsers import parse_gate_run
    payload = {"run_id": "r", "mode": "replay", "results": [{
        "scenario_id": "TASK-01",
        "trajectory": {"scenario_id": "TASK-01", "model": "m", "steps": [], "error": None},
        "checks": [{"name": "expected_tool:x", "passed": True},
                   {"name": "expected_tool:x", "passed": True}],
        "judge": None, "cost_usd": None}]}
    reading = parse_gate_run("EV", "r.json", payload)
    assert reading.facts["scenarios"]["TASK-01"]["behavioral_checks"] == 1


def test_a_bogus_valued_signature_block_is_not_present():
    """The six keys with arbitrary values (version 'bogus', algorithm 'none', a
    one-character digest) wear the shape of a signature and carry none of it."""
    r = parse_contract("EV", "c.json", {
        "contract_version": "deployment-contract.v1",
        "metadata": {"project": "p", "status": "approved",
                     "approval_signature": {"version": "bogus", "kind": "x", "algorithm": "none",
                        "public_key_fingerprint": "z", "digest_sha256": "x", "signature": "x"}},
        "requirements": [{"id": "REQ-001", "title": "t"}]}, "abc")
    assert r.facts.get("signature_present") is False


def test_canary_clean_report_with_forged_zero_counters_but_failing_replays_is_not_pass():
    """A forger can zero hard_failures and state_failures on a clean, safe report
    while replay.results still record failed replays; the failures are recomputed
    from the results, not trusted from the counters."""
    r = parse_blast_radius("EV", "b.json", {
        "format": "blast-radius.v1",
        "verdict": {"state": "clean", "safe": True, "hard_failures": 0, "state_failures": 0},
        "change_groups": [], "affected": [],
        "replay": {"invoked": True, "exit_codes": [1],
                   "results": [{"scenario_id": "TASK-08", "derived_passed": False,
                                "failed_checks": ["expected_tool:x"]}]}})
    assert r.status is Status.FAIL


def test_statediff_and_silobench_disagreeing_on_final_state_is_inconclusive():
    """A StateDiff verdict and a SiloBench verify for one task name different final
    states. There is no shared run id between the two producers, so a disagreement
    does not prove one execution ended two ways: it proves the two artifacts cannot
    be bound to a single final world. That is INCONCLUSIVE (the release goes
    INCOMPLETE), not a manufactured FAIL, and it still refuses to clear."""
    from revpack.parsers import cross_reference
    sd = parse_state_verdict("EV1", "sd.json", {
        "format": "statediff.verdict.v1", "scenario_id": "SD-PAY-01", "status": "pass",
        "checks": [{"id": "c", "status": "pass"}],
        "artifacts": {"after_state_hash": "a" * 64},
        "provenance": {"silobench_task": "TASK-10"}})
    sv = parse_silobench_verify("EV2", "sv.json", [{
        "task_id": "TASK-10", "verdict": {"passed": True, "failures": []},
        "profile": {"schema_version": 1, "docs_outage": False},
        "final_state_hash": "b" * 64, "is_default_run": True}])
    cross_reference([sd, sv])
    assert sv.facts["tasks"]["TASK-10"]["status"] == Status.INCONCLUSIVE.value
    assert sd.status is Status.INCONCLUSIVE
