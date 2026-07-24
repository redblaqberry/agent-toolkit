"""deployment-contract.v2: the data model.

A contract is only executable through its requirements: every KPI, role,
action, escalation rule, security constraint, SLO, environment, and
data-governance entry references a requirement id, and every requirement
traces to numbered transcript turns or an explicitly recorded follow-up.
The canonical JSON Schema in ``schemas/deployment-contract.v2.schema.json`` is
the wire format: it is the language-neutral definition a third party would
generate against, and these models mirror its fields and its mandatory keys.

They are not interchangeable gates, and the loader deliberately applies both.
The schema is stricter about JSON types (it rejects ``"3"`` where an integer is
required, which pydantic would coerce). The models are stricter about things
JSON Schema cannot express: calendar-valid dates (``2026-02-31`` satisfies the
date pattern and is not a day), the snake_case key pattern inside
``max_action_calls``, and text that is present but blank. A document has to
satisfy both to load, so the effective contract is the intersection; anything
entering through ``DeploymentContract.model_validate`` alone has skipped the
wire-format half.

v2 supersedes v1, which had no released consumers. It adds ``acceptance_rules``
(the typed primitives the acceptance suite is compiled from, replacing
domain-bound scenario templates), ``requirements[].out_of_band_verification``
(the explicit record that a promise is not checkable by an agent trajectory and
how it is verified instead), and ``metadata.system``; and it renames the cost
SLO from ``cost_per_invoice_eur`` to ``cost_per_task_eur`` with an explicit
``unit``, because a deployment contract is not an invoicing document.

Namespaces: ``roles[].permissions`` are human-side capabilities inside the
customer workflow; ``allowed_actions[].action`` is the exhaustive allowlist of
agent actions. They are separate namespaces that may reuse a word (a human and
the agent can both read an invoice); neither implies the other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

REQ_ID_PATTERN = r"^REQ-[0-9]{3}$"
UNK_ID_PATTERN = r"^UNK-[0-9]{3}$"
RULE_ID_PATTERN = r"^RULE-[0-9]{3}$"
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
SLUG_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


def _calendar_valid(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")  # rejects 2026-99-99 and 2026-02-31
    return value


def _non_blank(value: str) -> str:
    """Reject whitespace-only text.

    ``min_length=1`` accepts a single space, which is worse than an empty
    string here: a blank reason or verifier reads as a filled-in field all the
    way to the customer-facing report, and a blank output constraint is a check
    that every possible answer satisfies.
    """
    if not value.strip():
        raise ValueError("must not be blank or whitespace only")
    return value


NonBlank = Annotated[str, Field(min_length=1), AfterValidator(_non_blank)]
ReqId = Annotated[str, Field(pattern=REQ_ID_PATTERN)]
Name = Annotated[str, Field(pattern=NAME_PATTERN)]
TurnNumber = Annotated[int, Field(ge=1)]
DateStr = Annotated[str, Field(pattern=DATE_PATTERN), AfterValidator(_calendar_valid)]

RequirementCategory = Literal[
    "kpi", "role", "allowed_action", "escalation", "security",
    "latency", "cost", "data", "environment",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Resolution(StrictModel):
    decision: Literal["adopted", "rejected", "merged"]
    rationale: str = Field(min_length=1)
    resolved_by: str = Field(min_length=1)
    date: DateStr


class OutOfBandVerification(StrictModel):
    """Why a promise cannot be checked by an agent trajectory, and what does
    check it instead.

    Some commitments are real and load-bearing but simply not observable in a
    tool-call trace: data residency is a property of where the system is hosted,
    an append-only audit log is a property of the logging backend. Writing a
    scenario for them would produce a test that passes without proving
    anything. Recording them here instead keeps them visible: the acceptance
    suite states what it does not cover, and who signed off on covering it.
    """

    reason: NonBlank
    verified_by: NonBlank


class Requirement(StrictModel):
    id: str = Field(pattern=REQ_ID_PATTERN)
    title: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    category: RequirementCategory
    stakeholder: str = Field(min_length=1)
    source_turns: list[TurnNumber]
    followup_note: Optional[str] = None
    status: Literal["resolved", "conflict"]
    conflicts_with: list[ReqId] = Field(default_factory=list)
    resolution: Optional[Resolution] = None
    out_of_band_verification: Optional[OutOfBandVerification] = None


class QuestionResolution(StrictModel):
    answer: str = Field(min_length=1)
    resolved_by: str = Field(min_length=1)
    date: DateStr
    resulting_requirements: list[ReqId]


class OpenQuestion(StrictModel):
    id: str = Field(pattern=UNK_ID_PATTERN)
    question: str = Field(min_length=1)
    field: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    blocking: bool
    source_turns: list[TurnNumber] = Field(min_length=1)
    status: Literal["open", "resolved"]
    resolution: Optional[QuestionResolution]


class Kpi(StrictModel):
    name: Name
    target: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1)
    direction: Literal["at_least", "at_most"]
    requirement_id: ReqId


class Role(StrictModel):
    name: Name
    description: str = Field(min_length=1)
    permissions: list[Name] = Field(min_length=1)
    requirement_id: ReqId


class AllowedAction(StrictModel):
    action: Name
    allowed_roles: list[Name] = Field(min_length=1)
    requires_human_approval: bool
    requirement_id: ReqId


class EscalationRule(StrictModel):
    trigger: str = Field(min_length=1)
    action: str = Field(min_length=1)
    requirement_id: ReqId


class SecurityConstraint(StrictModel):
    constraint: str = Field(min_length=1)
    requirement_id: ReqId


class LatencySlo(StrictModel):
    value: int = Field(ge=1)
    requirement_id: ReqId


class CostSlo(StrictModel):
    value: float = Field(gt=0, allow_inf_nan=False)
    # what one unit of work is in this deployment ("invoice", "refund
    # request"). The ceiling is per unit, and the manager report names it, so
    # the number is never presented without saying what it is per.
    unit: NonBlank
    requirement_id: ReqId


class Slo(StrictModel):
    p95_latency_ms: Optional[LatencySlo]
    cost_per_task_eur: Optional[CostSlo]


class Environment(StrictModel):
    name: str = Field(min_length=1)
    seed: int
    requirement_id: ReqId


class DataGovernance(StrictModel):
    owner: str = Field(min_length=1)
    retention_days: int = Field(ge=1)
    region: str = Field(min_length=1)
    requirement_id: ReqId


class ExpectedAction(StrictModel):
    name: Name
    args_subset: dict = Field(default_factory=dict)


class Expectation(StrictModel):
    """The observable outcome of one acceptance rule.

    Everything here is checked deterministically against the recorded
    trajectory. A rule whose expectation is empty has no outcome that can fail,
    and the validator refuses it: a rubric does not substitute, because replay
    mode does not run the judge, so a rubric-only scenario would pass
    unconditionally in the one mode that runs without credentials.
    """

    actions: list[ExpectedAction] = Field(default_factory=list)
    actions_ordered: bool = False
    forbidden_actions: list[Name] = Field(default_factory=list)
    max_action_calls: dict[Name, int] = Field(default_factory=dict)
    output_contains: list[NonBlank] = Field(default_factory=list)
    output_excludes: list[NonBlank] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.actions
            or self.forbidden_actions
            or self.max_action_calls
            or self.output_contains
            or self.output_excludes
        )


RuleType = Literal[
    "required_action",   # the agent must take these actions
    "forbidden_action",  # the agent must never take these actions
    "escalation",        # under a stated condition, the agent must hand off
    "latency",           # exercises the interactive path the latency SLO covers
    "cost",              # exercises the path the per-task cost ceiling covers
]


class AcceptanceRule(StrictModel):
    """One typed, domain-independent acceptance primitive.

    A rule is the contract's own statement of how a requirement is checked:
    the situation, the input to send, and the outcome that must be observable.
    The compiler renders rules into gate scenarios mechanically, so the suite
    is a function of the contract rather than of a template that happens to
    know this customer's requirement ids.
    """

    id: str = Field(pattern=RULE_ID_PATTERN)
    slug: str = Field(pattern=SLUG_PATTERN)
    type: RuleType
    requirement_id: ReqId
    # further requirements this rule cites for provenance only, typically the
    # rejected side of a resolved conflict; unlike requirement_id they may be
    # rejected, because the point is to show what was decided against
    cites: list[ReqId] = Field(default_factory=list)
    label: NonBlank  # plain language, for the manager report
    # the Given/When/Then the exported scenario carries as its description
    given: NonBlank
    when: NonBlank
    then: NonBlank
    message: NonBlank  # sent verbatim to the agent under test
    # whether this exercises the interactive path the p95 latency SLO applies
    # to; batch and approver-side flows are excluded from that population
    interactive: bool = True
    expect: Expectation = Field(default_factory=Expectation)
    # judged criteria, and only ever in addition to expect: the judge does not
    # run in replay mode, so a rubric can never be a rule's only check
    rubric: list[NonBlank] = Field(default_factory=list)
    max_steps: Optional[int] = Field(default=None, ge=1)


class Metadata(StrictModel):
    project: str = Field(min_length=1)
    # human-readable name of the system under test, used wherever a document is
    # written for people ("supplier-invoice agent"); project stays the slug
    system: str = Field(min_length=1)
    customer: str = Field(min_length=1)
    transcript: str = Field(min_length=1)
    transcript_sha256: Optional[Annotated[str, Field(pattern=SHA256_PATTERN)]]
    status: Literal["draft", "approved"]
    approved_by: Optional[str]
    approved_at: Optional[DateStr]
    # Ed25519 attestation written by `approve --signing-key`; opaque to the
    # model (its shape is checked cryptographically in discoveryspec.attest),
    # excluded from its own digest. None on unsigned and draft contracts.
    approval_signature: Optional[dict] = None


class DeploymentContract(StrictModel):
    contract_version: Literal["deployment-contract.v2"]
    metadata: Metadata
    requirements: list[Requirement] = Field(min_length=1)
    open_questions: list[OpenQuestion]
    kpis: list[Kpi]
    roles: list[Role]
    allowed_actions: list[AllowedAction]
    escalation_rules: list[EscalationRule]
    security_constraints: list[SecurityConstraint]
    slo: Slo
    environment: Optional[Environment]
    data_governance: Optional[DataGovernance]
    acceptance_rules: list[AcceptanceRule] = Field(default_factory=list)

    def requirement_by_id(self) -> dict[str, Requirement]:
        return {r.id: r for r in self.requirements}

    def rules_by_requirement(self) -> dict[str, list[AcceptanceRule]]:
        index: dict[str, list[AcceptanceRule]] = {}
        for rule in self.acceptance_rules:
            index.setdefault(rule.requirement_id, []).append(rule)
        return index


# Categories that describe agent behavior, and can therefore be checked by an
# acceptance suite that observes a trajectory. The others are contract facts:
# a KPI is measured in production over time, roles and environments are
# configuration, and data governance is a hosting and retention property.
BEHAVIORAL_CATEGORIES = frozenset(
    {"allowed_action", "escalation", "security", "latency", "cost"}
)

# Sections whose entries constrain what the agent does at run time. Coverage is
# owed by anything wired into one of these as well as by anything in a
# behavioral category, because the category alone is a label the contract
# author chooses: security_constraints accepts a requirement filed as `data`,
# and without this a behavioral promise could be filed under a non-behavioral
# category and escape the requirement to be tested or explicitly excused.
BEHAVIORAL_SECTIONS = (
    "allowed_actions",
    "escalation_rules",
    "security_constraints",
    "slo.p95_latency_ms",
    "slo.cost_per_task_eur",
)
