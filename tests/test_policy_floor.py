"""The policy floor.

A purely declarative policy is trivially defeatable: a policy with no required
evidence and no rules satisfies "nothing failed" and clears every release. A
safety property a config file can switch off is decoration, so the floor lives
in code and validate_policy refuses anything that digs under it.

Note what this means for the safety-property test: varying only the evidence
cannot catch a vacuous policy, so the policy itself has to be varied too.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import assert_problem
from revpack.errors import PolicyError
from revpack.models import Policy, RequiredEvidence, Rule
from revpack.policy import validate_policy


def floor_policy(**overrides) -> Policy:
    payload = {
        "policy": "release-policy.v1",
        "id": "p",
        "required_evidence": [
            {"kind": "contract", "min_count": 1, "reduce": "all"},
            {"kind": "gate-run", "min_count": 1, "reduce": "worst"},
        ],
        "rules": [
            {"id": "R-007", "kind": "release_binding", "blocking": True},
            {"id": "R-002", "kind": "assertion_density", "blocking": True, "min_behavioral_checks": 1},
            {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
        ],
    }
    payload.update(overrides)
    return Policy.model_validate(payload)


def test_the_floor_policy_is_valid():
    validate_policy(floor_policy())


def test_empty_policy_is_rejected():
    # The vacuous case: no evidence, no rules, so nothing can ever fail.
    with pytest.raises(PolicyError) as exc:
        validate_policy(floor_policy(required_evidence=[], rules=[]))
    assert len(exc.value.problems) >= 4


def test_missing_contract_evidence_is_rejected():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(required_evidence=[{"kind": "gate-run", "min_count": 1, "reduce": "worst"}])
        )
    assert_problem(exc, "'contract' is always required")


def test_missing_gate_run_evidence_is_rejected():
    with pytest.raises(PolicyError):
        validate_policy(
            floor_policy(required_evidence=[{"kind": "contract", "min_count": 1, "reduce": "all"}])
        )


def test_zero_min_count_on_a_floor_kind_is_rejected():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                required_evidence=[
                    {"kind": "contract", "min_count": 0, "reduce": "all"},
                    {"kind": "gate-run", "min_count": 1, "reduce": "worst"},
                ]
            )
        )
    assert_problem(exc, "min_count >= 1")


def test_release_binding_cannot_be_made_advisory():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                rules=[
                    {"id": "R-007", "kind": "release_binding", "blocking": False},
                    {"id": "R-002", "kind": "assertion_density", "blocking": True},
                    {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
                ]
            )
        )
    assert_problem(exc, "must be blocking")


def test_assertion_density_cannot_be_made_advisory():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                rules=[
                    {"id": "R-007", "kind": "release_binding", "blocking": True},
                    {"id": "R-002", "kind": "assertion_density", "blocking": False},
                    {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
                ]
            )
        )
    assert_problem(exc, "must be blocking")


def test_assertion_density_cannot_require_zero_assertions():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                rules=[
                    {"id": "R-007", "kind": "release_binding", "blocking": True},
                    {
                        "id": "R-002",
                        "kind": "assertion_density",
                        "blocking": True,
                        "min_behavioral_checks": 0,
                    },
                    {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
                ]
            )
        )
    assert_problem(exc, "min_behavioral_checks >= 1")


def test_release_binding_rule_cannot_simply_be_omitted():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                rules=[
                    {"id": "R-002", "kind": "assertion_density", "blocking": True},
                    {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
                ]
            )
        )
    assert_problem(exc, "'release_binding' is always required")


def test_duplicate_rule_ids_are_rejected():
    with pytest.raises(PolicyError) as exc:
        validate_policy(
            floor_policy(
                rules=[
                    {"id": "R-007", "kind": "release_binding", "blocking": True},
                    {"id": "R-002", "kind": "assertion_density", "blocking": True},
                    {"id": "R-001", "kind": "requirement_coverage", "blocking": True},
                    {"id": "R-002", "kind": "state_verdict", "blocking": True},
                ]
            )
        )
    assert_problem(exc, "duplicate rule id")


# ---------------------------------------------------------------------------
# schema-level guards
# ---------------------------------------------------------------------------


def test_reduce_is_mandatory():
    # A silent default is how the ambiguity in the earlier design survived.
    with pytest.raises(ValidationError):
        RequiredEvidence.model_validate({"kind": "contract", "min_count": 1})


def test_unknown_reduce_mode_is_rejected():
    with pytest.raises(ValidationError):
        RequiredEvidence.model_validate({"kind": "contract", "min_count": 1, "reduce": "any"})


def test_authoritative_requires_a_selector():
    with pytest.raises(ValidationError, match="authoritative_evidence_id"):
        RequiredEvidence.model_validate(
            {"kind": "gate-run", "min_count": 1, "reduce": "authoritative"}
        )


def test_regression_rule_requires_a_metric():
    with pytest.raises(ValidationError, match="requires a metric"):
        Rule.model_validate({"id": "R-006", "kind": "regression"})


def test_unknown_rule_kind_is_rejected():
    with pytest.raises(ValidationError):
        Rule.model_validate({"id": "R-099", "kind": "vibes_check"})


def test_extra_fields_are_refused():
    # A silently ignored `blocking: true` is indistinguishable from a rule that
    # does not exist.
    with pytest.raises(ValidationError):
        Rule.model_validate({"id": "R-1", "kind": "state_verdict", "blockign": True})
