"""Planning contracts. Runtime identities/results are imported from the shared schema."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..contracts import Condition, Contract, ExecutionBudget, Identifier, Positive

Verdict = Literal["pass", "fail", "unknown"]
NodeStatus = Literal[
    "pending", "running", "awaiting_verification", "succeeded", "skipped_verified",
    "needs_recovery", "blocked", "superseded",
]
TaskStatus = Literal["active", "succeeded", "failed", "blocked", "budget_exhausted", "interrupted"]


class PlanningLimits(Contract):
    max_subgoals: Annotated[int, Field(gt=0, le=100)] = 16
    max_attempts_per_recovery_group: Positive = 3
    max_segments_per_attempt: Positive = 4
    max_replans: Positive = 4
    max_observations_without_progress: Positive = 3
    max_state_age_s: Annotated[float, Field(gt=0, le=3600)] = 30.0


class PredicateSpec(Contract):
    name: Identifier
    arity: Annotated[int, Field(ge=0, le=8)]
    description: str


class PredicateCatalog(Contract):
    catalog_version: Identifier
    predicates: list[PredicateSpec]


class Subgoal(Contract):
    id: Identifier
    goal: Annotated[str, Field(min_length=1)]
    depends_on: list[Identifier] = Field(default_factory=list)
    preconditions: list[Condition] = Field(default_factory=list)
    success_conditions: list[Condition] = Field(min_length=1)
    candidate_skills: list[Identifier] = Field(min_length=1)
    recovery_group_id: Identifier
    recovery_of: Identifier | None = None


class PlanProposal(Contract):
    based_on_state_version: Annotated[int, Field(ge=0)]
    goal_coverage: dict[str, list[Identifier]]
    subgoals: list[Subgoal] = Field(min_length=1)


class TaskPlan(PlanProposal):
    episode_id: Identifier
    task_id: Identifier
    task_version: Positive
    plan_version: Positive


class PlanPatch(Contract):
    base_plan_version: Positive
    base_progress_revision: Annotated[int, Field(ge=0)]
    based_on_state_version: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=1)]
    retire_subgoals: list[Identifier] = Field(default_factory=list)
    add_subgoals: list[Subgoal] = Field(default_factory=list)
    dependency_updates: dict[str, list[Identifier]] = Field(default_factory=dict)
    goal_coverage: dict[str, list[Identifier]] | None = None


class SegmentContract(Contract):
    segment_id: Identifier
    subgoal_id: Identifier
    attempt_id: Identifier
    entry_conditions: list[Condition]
    expected_conditions: list[Condition] = Field(min_length=1)
    continue_conditions: list[Condition] = Field(default_factory=list)
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)


class SubgoalProgress(Contract):
    status: NodeStatus = "pending"
    attempt_count: Annotated[int, Field(ge=0)] = 0
    active_attempt_id: Identifier | None = None
    execution_ids: list[Identifier] = Field(default_factory=list)
    verification_refs: list[str] = Field(default_factory=list)
    completed_at: str | None = None
    can_continue: bool = False
    last_failure: str | None = None


class TaskProgress(Contract):
    episode_id: Identifier
    task_status: TaskStatus = "active"
    plan_version: Positive
    revision: Annotated[int, Field(ge=0)] = 0
    active_subgoal_id: Identifier | None = None
    inflight_execution_id: Identifier | None = None
    subgoals: dict[str, SubgoalProgress]
    recovery_attempts: dict[str, int] = Field(default_factory=dict)
    attempt_segments: dict[str, int] = Field(default_factory=dict)
    replans: Annotated[int, Field(ge=0)] = 0
    observations_without_progress: Annotated[int, Field(ge=0)] = 0
    stop_reason: str | None = None
    final_verification_ref: str | None = None


class PlannerDecision(Contract):
    kind: Literal["execute_subgoal", "continue_subgoal", "request_observation", "verify_subgoal",
                  "verify_final", "revise_plan", "wait_for_execution", "stopped"]
    reason: str
    subgoal_id: Identifier | None = None


class VerificationRequest(Contract):
    request_id: Identifier
    episode_id: Identifier
    plan_version: Positive
    scope: Literal["subgoal", "final_goal"]
    subject_id: Identifier
    execution_id: Identifier | None
    groups: dict[str, list[Condition]]
    not_before: str | None
    min_state_version: Annotated[int, Field(ge=0)]


class ConditionCheck(Contract):
    condition: Condition
    verdict: Verdict
    source: Literal["observed", "mock", "predicted", "missing"]
    observed_at: str | None
    evidence_refs: list[str]


class VerificationReport(Contract):
    report_id: Identifier
    request_id: Identifier
    episode_id: Identifier
    plan_version: Positive
    scope: Literal["subgoal", "final_goal"]
    subject_id: Identifier
    execution_id: Identifier | None
    state_version: Annotated[int, Field(ge=0)]
    state_hash: str
    checked_at: str
    groups: dict[str, list[ConditionCheck]]
