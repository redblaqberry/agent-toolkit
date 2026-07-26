"""Regression guards for a round of fail-open findings in the producer parsers,
the pack seal, the policy floor, and release binding.

Each test names the hole it keeps shut. The shape is always the same: an
artifact, a pack, or a release descriptor that is malformed, unmoored, or
self-contradictory used to clear, and the safe reading is that it must not.
"""

from __future__ import annotations

import json
import math

import pytest

from conftest import (
    CLEAN,
    CLEAN_EVIDENCE,
    build_pack,
    load_yaml,
    read_json,
    write_json,
    write_yaml,
)
from revpack.errors import InputError, PackError
from revpack.lattice import Status
from revpack.pack import seal
from revpack.parsers import parse_blast_radius, parse_contract, parse_gate_run, parse_state_verdict


def _clean_contract() -> dict:
    return read_json(CLEAN / "contract" / "approved-contract.json")


def _clean_report() -> dict:
    return read_json(CLEAN / "blast-radius" / "report.json")


def _clean_run() -> dict:
    return read_json(CLEAN / "gate-run" / "run.json")


# --- H6: signature must be a 64-byte ed25519 value -------------------------

def test_signature_shorter_than_ed25519_is_not_present():
    """A base64 string that decodes to anything other than 64 bytes is not an
    ed25519 signature. "fake-signature-for-fixture-use-only" is valid base64 and
    35 bytes; without a length gate it satisfied `must: [signature_present]`."""
    c = _clean_contract()
    c["metadata"]["approval_signature"]["signature"] = (
        "ZmFrZS1zaWduYXR1cmUtZm9yLWZpeHR1cmUtdXNlLW9ubHk="  # 35 bytes decoded
    )
    r = parse_contract("EV", "contract.json", c, "0" * 64)
    assert r.facts["signature_present"] is False


def test_signature_of_exactly_64_bytes_is_present():
    c = _clean_contract()  # the fixture already carries a 64-byte value
    r = parse_contract("EV", "contract.json", c, "0" * 64)
    assert r.facts["signature_present"] is True


# --- H5: replay results must affirmatively pass ----------------------------

@pytest.mark.parametrize(
    "results",
    [
        [{}],  # an empty result records no verdict
        [{"derived_passed": "false"}],  # a string that `is False` never matches
        [{"derived_passed": None}],
        [{"scenario_id": "x"}],  # no derived_passed at all
        ["not-even-a-dict"],
    ],
)
def test_replay_result_that_does_not_affirmatively_pass_is_a_failure(results):
    """A `clean, safe` report with zero summary counters must not clear when its
    replay results do not each declare a pass in the affirmative."""
    b = _clean_report()
    b["verdict"]["state"] = "clean"
    b["verdict"]["safe"] = True
    b["verdict"]["hard_failures"] = 0
    b["verdict"]["state_failures"] = 0
    b["replay"]["results"] = results
    r = parse_blast_radius("EV", "report.json", b)
    assert r.status is Status.FAIL


def test_replay_result_that_affirmatively_passes_still_clears():
    b = _clean_report()
    b["replay"]["results"] = [{"scenario_id": "s", "derived_passed": True}]
    r = parse_blast_radius("EV", "report.json", b)
    assert r.status is Status.PASS


# --- H7: a non-string top-level agent_model is malformed -------------------

@pytest.mark.parametrize("model", [{}, 42, ["x"], {"name": "m"}])
def test_non_string_agent_model_is_refused(model):
    """binding reads agent_model as a string and skips it when it is not one, so a
    header of `{}` bypassed every model-consistency check by being unreadable."""
    g = _clean_run()
    g["agent_model"] = model
    with pytest.raises(InputError, match="agent_model"):
        parse_gate_run("EV", "run.json", g)


def test_absent_agent_model_is_allowed():
    g = _clean_run()
    g["agent_model"] = None
    parse_gate_run("EV", "run.json", g)  # must not raise


# --- H9: non-finite cost or latency is not a measurement -------------------

def _inject_first_step(run: dict, key: str, value) -> bool:
    for result in run.get("results", []):
        steps = (result.get("trajectory") or {}).get("steps") or []
        if steps:
            steps[0][key] = value
            return True
    return False


def test_nan_latency_does_not_clear_without_an_slo():
    g = _clean_run()
    assert _inject_first_step(g, "latency_s", float("nan"))
    r = parse_gate_run("EV", "run.json", g)
    assert r.status is not Status.PASS


def test_infinite_cost_does_not_clear_without_an_slo():
    g = _clean_run()
    for result in g.get("results", []):
        result["cost_usd"] = math.inf
    r = parse_gate_run("EV", "run.json", g)
    assert r.status is not Status.PASS


# --- H4: two StateDiff verdicts disagreeing about one task -----------------

def test_two_statediff_after_states_for_one_task_are_inconclusive():
    """Keying the after-state on the task silently kept whichever verdict was read
    last. Two verdicts for one task with different after-states leave no single
    established final world, so both go INCONCLUSIVE."""
    from revpack.parsers import cross_reference

    common = {
        "format": "statediff.verdict.v1", "status": "pass",
        "checks": [{"id": "c", "status": "pass"}],
        "provenance": {"silobench_task": "TASK-10"},
    }
    sd1 = parse_state_verdict("EV1", "sd1.json", {
        **common, "scenario_id": "SD-1", "artifacts": {"after_state_hash": "a" * 64}})
    sd2 = parse_state_verdict("EV2", "sd2.json", {
        **common, "scenario_id": "SD-2", "artifacts": {"after_state_hash": "b" * 64}})
    cross_reference([sd1, sd2])
    assert sd1.status is Status.INCONCLUSIVE
    assert sd2.status is Status.INCONCLUSIVE


# --- H1: seal refuses a file no envelope describes -------------------------

def test_seal_refuses_evidence_file_with_no_envelope(tmp_path, keys):
    """The decision is a function of the enveloped files only, while the manifest
    signs every file in the pack. A file dropped into evidence/ with no envelope is
    sealed and categorized as evidence yet never decided; seal must refuse it."""
    private, _ = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    smuggled = pack / "evidence" / "state-verdict" / "smuggled.json"
    smuggled.write_text(json.dumps({"format": "statediff.verdict.v1", "status": "fail"}),
                        encoding="utf-8")
    with pytest.raises(PackError, match="no envelope describes"):
        seal(pack, str(private))


# --- H2: a declared contract change requires a blast-radius report ---------

def test_declared_contract_change_without_blast_radius_is_incomplete(tmp_path):
    """Declaring an MCP contract change is a claim its consumer impact was
    assessed. A pack that declares the change but carries no canary report has
    assessed nothing, and must not clear."""
    release = load_yaml(CLEAN / "release.yaml")
    release["contract_change"] = {
        "declared": True,
        "baseline_fingerprint": "1" * 64,
        "candidate_fingerprint": "2" * 64,
    }
    release_path = write_yaml(tmp_path / "release.yaml", release)
    evidence = {k: v for k, v in CLEAN_EVIDENCE.items() if k != "blast-radius"}
    decision = build_pack(tmp_path / "pack", evidence=evidence, release=release_path)
    assert decision.state == "INCOMPLETE"
    assert any(r.id == "R-FLOOR-BLAST-RADIUS" for r in decision.rules)


# --- H3: a no-change report must be moored to a declared fingerprint -------

def test_no_change_without_declared_unchanged_fingerprint_is_inconclusive(tmp_path):
    """Two equal report fingerprints prove the report is self-consistent, not that
    it is about this release. Without a declared unchanged_fingerprint the report
    is unmoored, so the no-change declaration is uncorroborated."""
    release = load_yaml(CLEAN / "release.yaml")
    release["contract_change"] = {"declared": False}  # drop unchanged_fingerprint
    release_path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=release_path)
    assert decision.state != "GO"


def test_no_change_with_mismatched_unchanged_fingerprint_fails(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    release["contract_change"] = {"declared": False, "unchanged_fingerprint": "0" * 64}
    release_path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=release_path)
    assert decision.state == "NO_GO"


def test_clean_pack_with_declared_unchanged_fingerprint_still_goes(tmp_path):
    decision = build_pack(tmp_path / "pack")  # clean release declares the fingerprint
    assert decision.state == "GO"
