"""Shared, versioned contracts for the first fixed-script execution slice.

Planning and verification will consume these models rather than maintaining
separate execution enums. No model in this module declares task success.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Positive = Annotated[int, Field(gt=0)]
MotionState = Literal["idle", "running", "unknown"]
ExecutionStatus = Literal["completed", "error", "timed_out", "cancelled", "outcome_unknown"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Condition(Contract):
    id: Identifier
    predicate: Identifier
    args: list[Identifier]
    expected: bool = True


class Fact(Contract):
    predicate: Identifier
    args: list[Identifier]
    value: bool | None
    observed_at: str
    source: Literal["observed", "mock", "predicted"]
    evidence_refs: list[str] = Field(default_factory=list)


class StateView(Contract):
    episode_id: Identifier
    state_version: Annotated[int, Field(ge=0)]
    observed_at: str
    objects: list[Identifier]
    facts: list[Fact]
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_facts(self) -> StateView:
        keys = [(f.predicate, tuple(f.args)) for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("conflicting/duplicate facts must be resolved by the state provider")
        return self


class EpisodeBudget(Contract):
    max_executions: Positive = 10
    max_api_calls: Positive = 40


class ExecutionBudget(Contract):
    max_api_calls: Positive = 8
    max_wall_time_s: Annotated[float, Field(gt=0, le=300)] = 10.0
    max_frames: Annotated[int, Field(ge=0, le=512)] = 32


class TaskSpec(Contract):
    schema_version: Literal["0.1"] = "0.1"
    episode_id: Identifier
    task_id: Identifier
    task_version: Positive = 1
    instruction: Annotated[str, Field(min_length=1)]
    goal_conditions: list[Condition] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    budget: EpisodeBudget = Field(default_factory=EpisodeBudget)

    @model_validator(mode="after")
    def unique_goals(self) -> TaskSpec:
        ids = [condition.id for condition in self.goal_conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("goal condition IDs must be unique")
        return self


class ExecutionIdentity(Contract):
    episode_id: Identifier
    execution_id: Identifier
    task_version: Positive = 1
    plan_version: Positive = 1
    subgoal_id: Identifier
    attempt_id: Identifier
    segment_id: Identifier


class ExecutionRequest(ExecutionIdentity):
    schema_version: Literal["0.1"] = "0.1"
    catalog_version: Identifier
    based_on_state_version: Annotated[int, Field(ge=0)]
    code: Annotated[str, Field(min_length=1, max_length=16000)]
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    allowed_skills: list[Identifier] = Field(min_length=1)
    entry_conditions: list[Condition] = Field(default_factory=list)
    max_state_age_s: Annotated[float, Field(gt=0, le=3600)] = 30.0
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)

    @model_validator(mode="after")
    def bounded_inputs(self) -> ExecutionRequest:
        if len(self.code.encode()) > 16000:
            raise ValueError("code exceeds 16000 UTF-8 bytes")
        if len(canonical_json(self.inputs).encode()) > 65536:
            raise ValueError("inputs exceed 65536 bytes; store large data as external artifacts")
        if len(self.allowed_skills) != len(set(self.allowed_skills)):
            raise ValueError("allowed_skills must be unique")
        return self

    @property
    def code_hash(self) -> str:
        return digest(self.code)

    @property
    def request_hash(self) -> str:
        return digest(canonical_json(self.model_dump(mode="json")))


class SkillSpec(Contract):
    name: Identifier
    version: Identifier = "v1"
    signature: str
    description: str
    completion_semantics: Literal["function_return_only"] = "function_return_only"


class SkillCatalog(Contract):
    catalog_version: Identifier
    skills: list[SkillSpec]


class BackendProfile(Contract):
    backend_id: Identifier
    session_id: Identifier
    mode: Literal["mock", "simulation"]
    cooperative_deadline: bool
    cooperative_cancel: bool
    stop_confirmation: bool
    sampling: Literal["call_boundaries"] = "call_boundaries"
    isolation: Literal["trusted_in_process"] = "trusted_in_process"


class FrameRef(Contract):
    frame_id: Identifier
    execution_id: Identifier
    call_id: Identifier | None
    camera_id: Identifier
    observed_at: str
    source: Literal["observed", "mock"]
    boundary: Literal["before_execution", "after_call", "after_execution", "reconcile"]
    path: str
    sha256: str | None = None
    clock_domain: Literal["utc"] = "utc"


class FrameManifest(Contract):
    execution_id: Identifier
    sampling_policy_version: Literal["call_boundaries_v1"] = "call_boundaries_v1"
    frames: list[FrameRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ExecutionReport(ExecutionIdentity):
    schema_version: Literal["0.1"] = "0.1"
    report_id: Annotated[str, Field(min_length=1, max_length=160)]
    report_revision: Positive = 1
    catalog_version: Identifier
    state_version_before: Annotated[int, Field(ge=0)]
    code_hash: str
    request_hash: str
    status: ExecutionStatus
    runtime_rc: int | None
    execution_mode: Literal["trusted_in_process", "process"] = "trusted_in_process"
    started_at: str
    ended_at: str | None
    backend_motion_state: MotionState
    stop_reason: str
    stop_evidence_refs: list[str] = Field(default_factory=list)
    partial_effects_possible: bool
    api_calls: Annotated[int, Field(ge=0)]
    wall_time_s: Annotated[float, Field(ge=0)] | None
    evidence_refs: list[str] = Field(default_factory=list)
    frame_manifest_ref: str | None = None
    frame_manifest_hash: str | None = None
    state_after_ref: str | None = None
    result_ref: str | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    output_truncated: bool = False
    feedback_missing: list[str] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def terminal_requires_idle(self) -> ExecutionReport:
        if self.status != "outcome_unknown" and self.backend_motion_state != "idle":
            raise ValueError("a terminal report requires confirmed idle backend")
        if self.status == "completed" and self.runtime_rc != 0:
            raise ValueError("completed requires a normal Python return")
        return self
