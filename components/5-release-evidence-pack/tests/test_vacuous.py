"""Vacuous-truth defences.

The policy floor stops a policy from asking nothing. These tests cover the other
direction: evidence and inputs shaped so that the checks are technically
satisfied while nothing has actually been checked. "All zero requirements are
covered" is true and worthless, and a tool that answers GO to it is worse than
no tool, because it launders an empty claim into a signed artifact.
"""

from __future__ import annotations

import hashlib
import json

import yaml

from conftest import CLEAN, CLEAN_EVIDENCE, STAMP, load_yaml, write_yaml
from revpack.lattice import Status
from revpack.pack import collect, decide


def _degenerate_contract(tmp_path):
    payload = json.loads((CLEAN / "contract" / "approved-contract.json").read_text(encoding="utf-8"))
    payload["requirements"] = []
    payload["allowed_actions"] = []
    payload["escalation_rules"] = []
    payload["security_constraints"] = []
    payload["slo"] = {"p95_latency_ms": None, "cost_per_invoice_eur": None}
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _build(tmp_path, contract_path, sha, entries):
    trace = write_yaml(
        tmp_path / "trace.yaml",
        {
            "map": "traceability-map.v1",
            "project": "p",
            "contract_sha256": sha,
            "declared_by": "tester",
            "entries": entries,
        },
    )
    release = load_yaml(CLEAN / "release.yaml")
    release["contract_sha256"] = sha
    release_path = write_yaml(tmp_path / "release.yaml", release)

    evidence = dict(CLEAN_EVIDENCE)
    evidence["contract"] = contract_path
    collect(
        [(kind, str(path)) for kind, path in evidence.items()],
        str(CLEAN / "policy.yaml"),
        str(release_path),
        str(trace),
        tmp_path / "pack",
        collected_at=STAMP,
        record_vcs=False,
    )
    return decide(tmp_path / "pack")


def test_contract_with_no_requirements_cannot_yield_go(tmp_path):
    # Found by probing: this previously returned GO with the rule reporting
    # "all 0 requirements are mapped".
    contract, sha = _degenerate_contract(tmp_path)
    decision = _build(tmp_path, contract, sha, entries=[])
    assert decision.state != "GO"
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.INCONCLUSIVE
    assert "vacuous" in rule.detail


def test_empty_traceability_map_makes_coverage_vacuous_not_satisfied(tmp_path):
    contract, sha = _degenerate_contract(tmp_path)
    decision = _build(tmp_path, contract, sha, entries=[])
    gate = next(c for c in decision.evidence_classes if c.kind == "gate-run")
    assert gate.status is Status.INCONCLUSIVE
    assert "vacuous" in gate.detail


def test_a_real_contract_with_a_full_map_still_passes(tmp_path):
    # Guards against fixing the hole by breaking the happy path.
    from conftest import build_pack

    decision = build_pack(tmp_path / "pack")
    assert decision.state == "GO"
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.PASS
