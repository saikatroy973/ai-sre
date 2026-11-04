from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ResourceName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"),
]
ClusterName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"),
]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InvestigationRequest(StrictModel):
    question: Question
    cluster: ClusterName
    namespace: ResourceName
    service: ResourceName
    deployment: ResourceName


class Scope(StrictModel):
    tenant: str
    cluster: ClusterName
    namespace: ResourceName


class EvidenceKind(StrEnum):
    SERVICE = "service"
    DEPLOYMENT = "deployment"
    REPLICASETS = "replicasets"
    PODS = "pods"
    ENDPOINTSLICES = "endpointslices"


class Query(StrictModel):
    resource: EvidenceKind
    verb: Literal["get", "list"]
    name: str | None = None
    label_selector: str | None = None
    limit: int | None = None
    complete: bool = True


class Evidence(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    investigation_id: UUID
    scope: Scope
    kind: EvidenceKind
    source: Literal["fixture", "kubernetes"]
    query: Query
    collected_at: datetime = Field(default_factory=utc_now)
    status: Literal["ok", "error"] = "ok"
    error_code: str | None = None
    content: dict[str, Any]
    sha256: str = ""

    def digest(self) -> str:
        serialized = json.dumps(
            self.model_dump(mode="json", exclude={"sha256"}),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    @classmethod
    def capture(cls, **kwargs: Any) -> Evidence:
        evidence = cls(**kwargs)
        return evidence.model_copy(update={"sha256": evidence.digest()})


class Candidate(StrictModel):
    category: Literal["service_selector_mismatch", "undetermined"]
    evidence_ids: list[UUID] = Field(max_length=5)


class TokenUsage(StrictModel):
    provider: Literal["scale"] = "scale"
    model: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)


class Proposal(StrictModel):
    candidate: Candidate
    usage: TokenUsage | None = None


class VerifiedClaim(StrictModel):
    text: str
    evidence_ids: list[UUID]


class HistoricalLead(StrictModel):
    """Approved historical context. Always a lead, never current evidence."""

    record_id: UUID
    title: str
    summary: str
    diagnosis: Literal["service_selector_mismatch", "undetermined"]
    source_investigation_id: UUID | None = None
    source_room_id: str | None = None
    source_trace_id: str | None = None
    source_evidence_ids: list[UUID] = Field(default_factory=list)
    source_message_ids: list[UUID] = Field(default_factory=list)
    content_sha256: str = ""
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    trust: Literal["historical_lead_not_evidence"] = "historical_lead_not_evidence"


class Verification(StrictModel):
    passed: bool
    checks: list[str]
    claims: list[VerifiedClaim] = Field(default_factory=list)


class Report(StrictModel):
    outcome: Literal["supported", "insufficient_evidence", "partial"]
    diagnosis: Literal["service_selector_mismatch", "undetermined"]
    summary: str
    verification: Verification
    next_steps: list[str]
    limitations: list[str]
    usage: TokenUsage | None = None
    historical_context: list[HistoricalLead] = Field(default_factory=list)
    read_only: Literal[True] = True


class AgentRole(StrEnum):
    COORDINATOR = "coordinator"
    INVESTIGATOR = "cluster-investigator"
    TELEMETRY_ANALYST = "telemetry-analyst"
    CHANGE_ANALYST = "change-analyst"
    MEMORY_RESEARCHER = "memory-researcher"
    VERIFIER = "evidence-verifier"
    OPERATOR = "operator"


class AgentMessage(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    investigation_id: UUID
    task_id: UUID
    parent_task_id: UUID | None = None
    sender: AgentRole
    recipient: AgentRole
    type: Literal[
        "task_request", "task_result", "verification_request", "verification_result", "report"
    ]
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    created_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any]


class InvestigationState(StrEnum):
    RECEIVED = "received"
    AUTHORIZED = "authorized"
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    VERIFYING = "verifying"
    REPORTED = "reported"
    FAILED = "failed"


class Investigation(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    principal_id: str
    scope: Scope
    request: InvestigationRequest
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    state: InvestigationState = InvestigationState.RECEIVED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    report: Report | None = None
    error_code: str | None = None
    switch_room_id: str | None = None
    switch_room_name: str | None = None
    switch_base_url: str | None = None
