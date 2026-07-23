"""Malformed input must produce a clean exit 2, never a traceback.

The tool's contract is that exit 2 means "could not decide or could not run".
An unhandled exception breaks that contract: a CI job cannot distinguish a crash
from a refusal, and a crash on malformed evidence is indistinguishable from a
crash on a bug.
"""

from __future__ import annotations

import pytest

from conftest import CLEAN, CLEAN_EVIDENCE, STAMP, build_pack
from revpack.errors import InputError, PackError
from revpack.lattice import Status
from revpack.parsers import load_json, parse_gate_run


def test_non_utf8_bytes_raise_a_clean_input_error(tmp_path):
    # UnicodeDecodeError is neither an OSError nor a JSONDecodeError, so it
    # escapes the obvious except clauses.
    path = tmp_path / "bad.json"
    path.write_bytes(b'{"a": "\xff\xfe invalid"}')
    with pytest.raises(InputError, match="not valid UTF-8"):
        load_json(path)


def test_malformed_json_raises_a_clean_input_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InputError, match="not valid JSON"):
        load_json(path)


def test_missing_file_raises_a_clean_input_error(tmp_path):
    with pytest.raises(InputError, match="cannot read"):
        load_json(tmp_path / "nope.json")


def test_collect_rejects_a_directory_given_as_evidence(tmp_path):
    evidence = dict(CLEAN_EVIDENCE)
    evidence["contract"] = CLEAN
    with pytest.raises(InputError, match="is not a file"):
        build_pack(tmp_path / "pack", evidence=evidence)


def test_two_artifacts_of_one_kind_with_the_same_filename_are_refused(tmp_path):
    # They would land on the same path inside the pack and one would silently
    # overwrite the other.
    duplicate = tmp_path / "elsewhere" / "run.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes((CLEAN / "gate-run" / "run.json").read_bytes())
    sources = list(CLEAN_EVIDENCE.items()) + [("gate-run", duplicate)]
    from revpack.pack import collect

    with pytest.raises(InputError, match="collides"):
        collect(
            [(k, str(v)) for k, v in sources],
            str(CLEAN / "policy.yaml"),
            str(CLEAN / "release.yaml"),
            str(CLEAN / "traceability.yaml"),
            tmp_path / "pack",
            collected_at=STAMP,
            record_vcs=False,
        )


def test_gate_run_that_is_not_an_object_is_refused():
    with pytest.raises(InputError, match="must be a JSON object"):
        parse_gate_run("EV-1", "run.json", [1, 2, 3])


def test_gate_run_with_no_results_is_inconclusive_not_pass():
    reading = parse_gate_run("EV-1", "run.json", {"run_id": "r", "mode": "live", "results": []})
    assert reading.status is Status.INCONCLUSIVE


def test_trajectory_without_a_scenario_id_is_unverifiable_not_pass():
    # Identity cannot be confirmed, which is not the same as confirming it is
    # wrong, but it is certainly not a pass.
    payload = {
        "run_id": "r",
        "mode": "live",
        "results": [
            {
                "scenario_id": "TASK-01",
                "trajectory": {"model": "m", "steps": [], "final_text": "", "error": None},
                "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                "judge": None,
                "cost_usd": None,
            }
        ],
    }
    reading = parse_gate_run("EV-1", "run.json", payload)
    assert reading.status is Status.INCONCLUSIVE
    assert reading.facts["scenarios"]["TASK-01"]["id_binding"] == "unverifiable"


def test_unverifiable_identity_fails_binding_as_inconclusive(tmp_path):
    import json

    from revpack.binding import bind
    from revpack.models import ReleaseDescriptor
    from conftest import load_yaml

    payload = {
        "run_id": "r",
        "mode": "live",
        "results": [
            {
                "scenario_id": "TASK-01",
                "trajectory": {"model": "claude-opus-4-8", "steps": [], "final_text": "", "error": None},
                "checks": [{"name": "expected_tool:x", "passed": True, "detail": ""}],
                "judge": None,
                "cost_usd": None,
            }
        ],
    }
    reading = parse_gate_run("EV-1", "run.json", payload)
    release = ReleaseDescriptor.model_validate(load_yaml(CLEAN / "release.yaml"))
    outcomes = bind(release, [reading])
    check = next(o for o in outcomes if o.check == "scenario_id_matches_trajectory")
    assert check.status is Status.INCONCLUSIVE


def test_verify_on_a_missing_pack_is_a_clean_error(tmp_path):
    from revpack.pack import verify

    with pytest.raises(PackError, match="not sealed"):
        verify(tmp_path, "nonexistent.pem")
