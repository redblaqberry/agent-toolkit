"""deployment-contract.v1: the data model.

A contract is only executable through its requirements: every KPI, role,
action, escalation rule, security constraint, SLO, environment, and
data-governance entry references a requirement id, and every requirement
traces to numbered transcript turns or an explicitly recorded follow-up.
The canonical JSON Schema lives in ``schemas/deployment-contract.v1.schema.json``;
these models mirror it field for field, including which keys are mandatory,
so the model and the schema accept the same documents.

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
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


def _calendar_valid(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")  # rejects 2026-99-99 and 2026-02-31
    return value


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
    requirement_id: ReqId


class Slo(StrictModel):
    p95_latency_ms: Optional[LatencySlo]
    cost_per_invoice_eur: Optional[CostSlo]


class Environment(StrictModel):
    name: str = Field(min_length=1)
    seed: int
    requirement_id: ReqId


class DataGovernance(StrictModel):
    owner: str = Field(min_length=1)
    retention_days: int = Field(ge=1)
    region: str = Field(min_length=1)
    requirement_id: ReqId


class Metadata(StrictModel):
    project: str = Field(min_length=1)
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
    contract_version: Literal["deployment-contract.v1"]
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

    def requirement_by_id(self) -> dict[str, Requirement]:
        return {r.id: r for r in self.requirements}
