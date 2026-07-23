"""Conformance: parse artifacts the real producers actually emitted.

Every other test in this suite runs against fixtures written by hand to match
this project's understanding of five upstream schemas. That proves internal
consistency and nothing about whether the understanding is right. A parser can
be perfectly self-consistent and still wrong about the format it claims to read.

These tests close that gap by parsing genuine producer output found in the
sibling repositories, and by driving StateDiff's real CLI to generate a verdict
on the spot. They are the tests that fail when an upstream tool changes shape.

They skip when the sibling repositories are absent (a fresh clone, or CI without
the whole workspace) rather than failing, because their absence is a fact about
the environment, not a defect in this code. A skipped conformance test is
reported loudly enough that nobody mistakes it for a pass: see
`test_conformance_corpus_is_reachable`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (
    FDE_ROOT,
    UPSTREAM_ORIGINS,
    VENDORED,
    read_json,
    real_artifact,
    redact_machine_paths,
)
from revpack.canonical import file_sha256
from revpack.lattice import Status
from revpack.parsers import cross_reference, parse

DISCOVERYSPEC = "discoveryspec-approved-contract.json"
CANARY_REPORT = "canary-report.json"
SILOBENCH_GOLDEN = "silobench-golden-hashes.json"
GATE_CLERK = "gate-run-clerk.json"
GATE_APPROVER = "gate-run-approver.json"
GATE_V2 = "gate-run-v2-clerk.json"
STATEDIFF_VERDICT = "statediff-verdict-SD-PAY-01.json"


def parse_real(kind: str, name: str):
    """Parse a vendored artifact that a real producer emitted.

    Vendored rather than read from the sibling repos so these run in CI, where
    the wider workspace does not exist. `test_vendored_corpus_matches_upstream`
    guards the copies against drift.
    """
    path = VENDORED / name
    assert path.is_file(), f"vendored conformance artifact missing: {path}"
    return parse("EV-REAL", kind, path, file_sha256(path))


def test_conformance_corpus_is_present():
    """The corpus ships with the repo, so its absence is a defect, not a skip."""
    missing = [
        name
        for name in (
            DISCOVERYSPEC, CANARY_REPORT, SILOBENCH_GOLDEN,
            GATE_CLERK, GATE_APPROVER, GATE_V2, STATEDIFF_VERDICT,
        )
        if not (VENDORED / name).is_file()
    ]
    assert not missing, f"vendored conformance corpus is incomplete: {missing}"


@pytest.mark.parametrize("name,origin", sorted(UPSTREAM_ORIGINS.items()))
def test_vendored_corpus_matches_upstream(name, origin):
    """Drift guard: a vendored copy must still equal what the producer emits.

    Skips when the sibling workspace is absent, because a fresh clone cannot
    know. A failure here means an upstream tool changed its output and this
    project has not caught up, which is exactly the signal worth having.

    Both sides are compared after path redaction, because the vendored copies
    are redacted before publication (see redact_machine_paths and
    fixtures/conformance/PROVENANCE.md). Redacting both sides keeps the guard
    blind to the author's directory layout and to nothing else: any change to
    a verdict, finding, count, or hash still fails it. The comparison is over
    parsed structure rather than bytes, so upstream reformatting alone does
    not raise a false alarm.
    """
    upstream = FDE_ROOT / "jobprojects" / origin
    if not upstream.is_file():
        pytest.skip(f"upstream origin not available: {upstream}")
    assert redact_machine_paths(read_json(VENDORED / name)) == redact_machine_paths(
        read_json(upstream)
    ), f"{name} has drifted from {origin}; re-vendor it and re-check the parsers"


def test_vendored_statediff_verdict_parses():
    """The verdict captured from StateDiff's real CLI."""
    reading = parse_real("state-verdict", STATEDIFF_VERDICT)
    assert reading.status is Status.PASS
    assert reading.facts["scenario_id"] == "SD-PAY-01"
    assert reading.facts["silobench_task"] == "TASK-10"
    assert reading.facts["requirements"] == ["REQ-007", "REQ-012"]


def test_vendored_statediff_hash_matches_vendored_silobench_golden():
    """Two tools, two languages, one hash they both agree on.

    StateDiff (Python) records the before-state it graded; SiloBench
    (TypeScript) commits the seeded initial hash. Release binding depends on
    that agreement, so it is pinned against both real artifacts rather than
    against fixtures written to match.
    """
    state = parse_real("state-verdict", STATEDIFF_VERDICT)
    golden = parse_real("environment-golden", SILOBENCH_GOLDEN)
    assert state.facts["before_state_hash"] == golden.facts["initial"]["v1"]
    assert state.facts["after_state_hash"] == golden.facts["tasks"]["TASK-10"]


# ---------------------------------------------------------------------------
# DiscoverySpec
# ---------------------------------------------------------------------------


def test_real_approved_contract_parses():
    reading = parse_real("contract", DISCOVERYSPEC)
    assert reading.status is Status.PASS
    # The real contract carries 19 requirements, REQ-001 to REQ-019.
    assert len(reading.facts["requirement_ids"]) == 19
    assert "REQ-007" in reading.facts["requirement_ids"]
    assert reading.facts["project"] == "nordlicht-invoice-automation"


def test_real_contract_is_correctly_reported_as_unsigned():
    # The committed example is approved but carries no approval_signature, so
    # its approved status is a structural claim only. Reporting it as signed
    # would be the exact overclaim this project exists to catch.
    reading = parse_real("contract", DISCOVERYSPEC)
    assert reading.facts["signature_present"] is False
    assert any("unauthenticated" in p for p in reading.problems)


def test_real_contract_slo_shape_is_understood():
    reading = parse_real("contract", DISCOVERYSPEC)
    slo = reading.facts["slo"]
    assert slo["p95_latency_ms"]["value"] == 2000
    assert slo["cost_per_invoice_eur"]["value"] == 0.08


# ---------------------------------------------------------------------------
# agent-eval-gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative,expected_scenarios", [(GATE_CLERK, 10), (GATE_APPROVER, 2)]
)
def test_real_gate_runs_parse_with_the_expected_scenario_counts(relative, expected_scenarios):
    # The SiloBench golden set is split by principal: 10 clerk, 2 approver.
    reading = parse_real("gate-run", relative)
    assert len(reading.facts["scenarios"]) == expected_scenarios


def test_real_gate_run_passes_and_every_scenario_asserts_something():
    reading = parse_real("gate-run", GATE_CLERK)
    assert reading.status is Status.PASS
    for scenario_id, info in reading.facts["scenarios"].items():
        assert info["behavioral_checks"] >= 1, f"{scenario_id} asserts nothing"


def test_real_gate_run_scenario_ids_are_the_silobench_task_set():
    clerk = parse_real("gate-run", GATE_CLERK).facts["scenarios"]
    approver = parse_real("gate-run", GATE_APPROVER).facts["scenarios"]
    assert set(clerk) | set(approver) == {f"TASK-{n:02d}" for n in range(1, 13)}


def test_real_failing_gate_run_is_reported_as_failing():
    # The v2 candidate run genuinely breaks against the changed contracts.
    assert parse_real("gate-run", GATE_V2).status is Status.FAIL


def test_real_gate_run_carries_no_verdict_field():
    """Pins the upstream fact this project's whole gate parser exists for."""
    payload = json.loads(real_artifact(GATE_CLERK).read_text(encoding="utf-8"))
    assert "verdict" not in payload
    assert "pass_rate" not in payload
    for result in payload["results"]:
        assert "passed" not in result
        assert "score" not in result


# ---------------------------------------------------------------------------
# MCP Contract Canary
# ---------------------------------------------------------------------------


def test_real_blast_radius_report_parses_as_breaking():
    reading = parse_real("blast-radius", CANARY_REPORT)
    assert reading.status is Status.FAIL
    assert reading.facts["state"] == "breaking"
    assert reading.facts["breaking"] >= 1


def test_real_blast_radius_keeps_per_scenario_requirement_claims():
    reading = parse_real("blast-radius", CANARY_REPORT)
    assert reading.facts["affected_map"], "no affected scenarios extracted"
    for scenario, reqs in reading.facts["affected_map"].items():
        assert scenario.startswith("TASK-")
        assert all(r.startswith("REQ-") for r in reqs)


# ---------------------------------------------------------------------------
# SiloBench
# ---------------------------------------------------------------------------


def test_real_golden_hashes_parse():
    reading = parse_real("environment-golden", SILOBENCH_GOLDEN)
    assert reading.status is Status.NOT_APPLICABLE
    assert set(reading.facts["tasks"]) == {f"TASK-{n:02d}" for n in range(1, 13)}
    assert len(reading.facts["initial"]["v1"]) == 64


def test_six_golden_task_hashes_are_identical():
    """Pins the upstream quirk that makes state-hash binding weak.

    The read-only tasks all end at the seeded initial state, so a state hash
    alone cannot distinguish them. This is the concrete reason release binding
    reports environment-level identity rather than run-level identity, and if it
    ever stops being true the honesty note in binding.py should be revisited.
    """
    reading = parse_real("environment-golden", SILOBENCH_GOLDEN)
    initial_v1 = reading.facts["initial"]["v1"]
    matching = [t for t, h in reading.facts["tasks"].items() if h == initial_v1]
    assert len(matching) == 6, f"expected 6 read-only tasks, got {sorted(matching)}"


# ---------------------------------------------------------------------------
# StateDiff, driven live
# ---------------------------------------------------------------------------


def find_statediff():
    """Locate StateDiff's CLI without assuming an operating system, or None.

    A hardcoded `.venv/Scripts/statediff.exe` exists only on Windows, so the one
    test in this suite that runs a real producer skipped on every Linux host,
    sibling repo checked out and a perfectly good virtualenv beside it.

    PATH first, because an installed entry point is what an operator would
    invoke, then whichever layout this platform's virtualenv builds.

    Returning None rather than skipping is what makes this testable. A resolver
    that decides its own caller's fate can only be checked by a test that skips
    when it is wrong, and a skipping test is indistinguishable from a passing
    one in every summary anybody reads.
    """
    found = shutil.which("statediff")
    if found:
        return Path(found)
    venv = FDE_ROOT / "jobprojects" / "03-statediff" / ".venv"
    for candidate in (venv / "Scripts" / "statediff.exe", venv / "bin" / "statediff"):
        if candidate.is_file():
            return candidate
    return None


def statediff_cli():
    exe = find_statediff()
    if exe is None:
        pytest.skip("statediff is neither on PATH nor installed in the sibling repo's venv")
    return exe


def test_the_statediff_cli_is_found_on_a_posix_venv_layout(tmp_path, monkeypatch):
    """The live producer test must not be reachable only from one machine.

    The executable was looked for at `.venv/Scripts/statediff.exe`, a path that
    exists on Windows and nowhere else, so the one test in this suite that runs
    a real producer skipped on every Linux host, sibling repo checked out and
    virtualenv working. A test that can only run where its author sits reports
    green everywhere else while checking nothing, which is the same shape as a
    check that quietly does not run.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys.modules[__name__], "FDE_ROOT", tmp_path)
    exe = tmp_path / "jobprojects" / "03-statediff" / ".venv" / "bin" / "statediff"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_statediff() == exe


def test_an_installed_statediff_on_path_is_used(tmp_path, monkeypatch):
    # An installed entry point is what an operator would invoke, so it is tried
    # before any assumption about where a virtualenv lives.
    installed = tmp_path / "statediff"
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: str(installed) if name == "statediff" else None)
    monkeypatch.setattr(sys.modules[__name__], "FDE_ROOT", tmp_path / "absent")
    assert find_statediff() == installed


def test_live_statediff_still_emits_what_we_vendored(tmp_path):
    """Re-run the real producer and compare against the vendored copy.

    The only test that executes an upstream tool. The vendored corpus proves the
    parsers handle real output; this proves the real output has not changed
    since it was captured. A failure means StateDiff's format moved, which is a
    signal to re-vendor and re-check, not a defect in this code.
    """
    root = FDE_ROOT / "jobprojects" / "03-statediff"
    if not root.is_dir():
        # The scenario and snapshot arguments below are relative to the sibling
        # repo, so an executable found on PATH is not enough on its own.
        pytest.skip(f"the statediff repository is not available at {root}")
    exe = statediff_cli()
    result = subprocess.run(
        [
            str(exe), "check",
            "--scenario", "scenarios/payment-release.yaml",
            "--before", "fixtures/baseline/payment/before-snapshot.json",
            "--after", "fixtures/baseline/payment/after-snapshot.json",
            "--json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"statediff exited {result.returncode}: {result.stderr[:400]}"

    fresh = tmp_path / "fresh.json"
    fresh.write_text(result.stdout, encoding="utf-8", newline="\n")
    live = parse("EV-LIVE", "state-verdict", fresh, file_sha256(fresh))
    vendored = parse_real("state-verdict", STATEDIFF_VERDICT)

    assert live.status is vendored.status
    assert live.facts == vendored.facts


# ---------------------------------------------------------------------------
# cross-producer, all real
# ---------------------------------------------------------------------------


def test_real_artifacts_cross_reference_without_error():
    readings = [
        parse_real("gate-run", GATE_CLERK),
        parse_real("environment-golden", SILOBENCH_GOLDEN),
        parse_real("blast-radius", CANARY_REPORT),
        parse_real("contract", DISCOVERYSPEC),
    ]
    cross_reference(readings)
    assert all(r.status is not None for r in readings)


def test_the_real_canary_map_agrees_with_the_real_contract():
    """Cross-artifact provenance, checked by meaning rather than by resolution.

    This check previously documented a live defect: the canary's consumer map
    claimed TASK-12 exercises REQ-012, the append-only audit-log rule, when the
    duplicate escalation TASK-12 actually tests is REQ-016. Every id resolved,
    so referential integrity passed all twelve and hid it. That is the whole
    argument for reading across repositories: no single project's test suite
    could see the mismatch, because each one validated only its own file.

    Repo 04's map has since been re-authored and this now asserts the corrected
    state. It is kept as a mapping test rather than deleted, because the defect
    class it guards against returns whenever a requirement id is chosen by
    position instead of by what the requirement says.
    """
    canary = parse_real("blast-radius", CANARY_REPORT)
    contract = parse_real("contract", DISCOVERYSPEC)
    titles = contract.facts["requirement_titles"]

    claimed = canary.facts["affected_map"].get("TASK-12")
    if not claimed:
        pytest.skip("the real canary report does not mention TASK-12")
    # TASK-12 is duplicate detection and escalation, so it must cite the
    # duplicate rule and not the audit-log rule.
    assert "REQ-016" in claimed
    assert "duplicate" in titles["REQ-016"].lower()
    assert "REQ-012" not in claimed
    assert "audit" in titles["REQ-012"].lower()
    # Every cited id still resolves. That was always true, which is exactly
    # why resolution alone was never evidence the mapping was right.
    assert set(claimed) <= set(titles)


def test_redaction_actually_removes_machine_paths():
    """The redactor must not be able to fail silently.

    A pattern that matches nothing leaves every path in place while the caller
    believes the corpus was cleaned, which is the same fail-open shape this
    project refuses everywhere else. So assert on the outcome, not on the
    implementation: redacting a known-bad path must change it.
    """
    samples = [
        r"C:\Users\someone\AppData\Local\Temp\tool-abc123\run-1.json",
        r"D:\a workspace\FDE\agent-eval-gate\examples\scenarios.yaml",
        r"D:\a workspace\FDE\jobprojects\04-mcp-contract-canary\report.json",
        "/home/someone/FDE/jobprojects/03-statediff/fixtures/x.json",
    ]
    for original in samples:
        redacted = redact_machine_paths(original)
        assert redacted != original, f"redaction silently did nothing to {original}"
        assert redacted.startswith(("<tmp>/", "<workspace>/")), redacted
    # A path with nothing machine-specific in it is left alone.
    assert redact_machine_paths("fixtures/clean/policy.yaml") == "fixtures/clean/policy.yaml"


def test_no_vendored_artifact_carries_a_machine_path():
    """Whole-corpus guard: nothing under fixtures/ may name a real machine."""
    forbidden = re.compile(r"AI PROJECTS|[A-Za-z]:\\Users|/home/|AppData", re.I)
    offenders = []
    for path in sorted((VENDORED.parent).rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if forbidden.search(text):
            offenders.append(path.name)
    assert not offenders, f"machine paths present in published fixtures: {offenders}"
