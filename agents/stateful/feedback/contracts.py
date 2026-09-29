"""CoF-inspired evidence contracts. Runtime identities remain shared."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..contracts import Condition, Contract, ExecutionIdentity, Identifier, StateView


class FeedbackLimits(Contract):
    max_analyses: Annotated[int, Field(ge=1, le=200)] = 32
    max_attempts_per_request: Annotated[int, Field(ge=1, le=3)] = 2
    max_frames: Annotated[int, Field(ge=2, le=64)] = 8
    max_state_records: Annotated[int, Field(ge=2, le=128)] = 32
    max_evidence_bytes: Annotated[int, Field(ge=4096, le=33554432)] = 4194304


class EvidenceRecord(Contract):
    evidence_id: Identifier
    kind: Literal["frame", "state"]
    path: str
    sha256: str
    observed_at: str
    boundary: Literal["before_execution", "after_call", "after_execution", "reconcile"]
    source: Literal["mock", "observed"]
    call_id: Identifier | None = None
    camera_id: Identifier | None = None
    clock_domain: Literal["utc"] = "utc"
    state: StateView | None = None


class AnalysisQuery(Contract):
    query_id: Identifier
    condition: Condition
    scope: Literal["at_end", "occurred", "maintained", "stable_for_window"] = "at_end"


class CoFRequest(ExecutionIdentity):
    request_id: Identifier
    report_revision: int
    report_hash: str
    input_hash: str
    event_watermark: int
    objects: list[Identifier]
    predicates: dict[str, int]
    queries: list[AnalysisQuery]
    evidence: list[EvidenceRecord]
    gaps: list[str]
    policy_version: Literal["cof-boundary-v1"] = "cof-boundary-v1"


class EvidenceClaim(Contract):
    claim_id: Identifier
    predicate: Identifier
    args: list[Identifier]
    value: bool | None
    observed_at: str
    evidence_refs: list[Identifier] = Field(min_length=1, max_length=16)
    unknown_reason: Annotated[str, Field(max_length=1000)] | None = None

    @model_validator(mode="after")
    def explain_unknown(self):
        if self.value is None and not self.unknown_reason:
            raise ValueError("unknown claims require a reason")
        return self


class CoFProposal(Contract):
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=1024)


class ConditionEvidence(Contract):
    query_id: Identifier
    condition_id: Identifier
    assessment: Literal["supported", "refuted", "unknown"]
    evidence_refs: list[Identifier]
    reason: str


class TimelineEvent(Contract):
    predicate: Identifier
    args: list[Identifier]
    before: bool
    after: bool
    started_at: str
    ended_at: str
    evidence_refs: list[Identifier]
    description: str


class CoFFeedback(ExecutionIdentity):
    feedback_id: Identifier
    request_id: Identifier
    report_revision: int
    input_hash: str
    event_watermark: int
    analysis_status: Literal["ready", "partial", "unavailable"]
    claims: list[EvidenceClaim]
    terminal_claims: list[EvidenceClaim]
    events: list[TimelineEvent]
    condition_evidence: list[ConditionEvidence]
    uncertainties: list[str]
    observation_requests: list[str]
    analyzer: str
    selected_frames: int
    selected_state_records: int
    elapsed_s: float
