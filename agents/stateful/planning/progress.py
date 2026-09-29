"""Rebuild progress from the execution ledger; snapshots are disposable exports."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import ExecutionReport, ExecutionRequest
from ..execution.backend import ExecutionFault
from ..execution.store import EventStore
from .contracts import (
    SegmentContract, SubgoalProgress, TaskPlan, TaskProgress, VerificationReport, VerificationRequest,
)


@dataclass
class PlanningState:
    plan: TaskPlan | None = None
    progress: TaskProgress | None = None
    requests: dict[str, ExecutionRequest] = field(default_factory=dict)
    segments: dict[str, SegmentContract] = field(default_factory=dict)
    reports: dict[str, ExecutionReport] = field(default_factory=dict)
    verification_requests: dict[str, VerificationRequest] = field(default_factory=dict)
    verifications: dict[str, VerificationReport] = field(default_factory=dict)
    verification_seq: dict[str, int] = field(default_factory=dict)
    last_observation_seq: int = 0


def restore(store: EventStore) -> PlanningState:
    return restore_events(store.events())


def restore_events(events: list[dict]) -> PlanningState:
    result = PlanningState()
    progress = None
    for event in events:
        kind, data, eid = event["kind"], event["payload"], event["execution_id"]
        if kind == "PlanCreated":
            if result.plan is not None:
                raise ExecutionFault("CORRUPT_PLANNING_LOG", "multiple initial plans")
            result.plan = TaskPlan.model_validate(data["plan"])
            progress = TaskProgress(episode_id=result.plan.episode_id, plan_version=1,
                                    subgoals={node.id: SubgoalProgress() for node in result.plan.subgoals}).model_dump()
        elif progress is None:
            continue
        elif kind == "ExecutionDispatched":
            if data.get("planning_segment") is None:
                raise ExecutionFault("CORRUPT_PLANNING_LOG", "unplanned dispatch after plan creation")
            request = ExecutionRequest.model_validate(data["request"])
            segment = SegmentContract.model_validate(data["planning_segment"])
            result.requests[eid], result.segments[eid] = request, segment
            node = next(node for node in result.plan.subgoals if node.id == request.subgoal_id)
            item = progress["subgoals"][node.id]
            if item["active_attempt_id"] != request.attempt_id:
                item["attempt_count"] += 1
                group = node.recovery_group_id
                progress["recovery_attempts"][group] = progress["recovery_attempts"].get(group, 0) + 1
            item.update(status="running", active_attempt_id=request.attempt_id, can_continue=False)
            item["execution_ids"].append(eid)
            progress["attempt_segments"][request.attempt_id] = progress["attempt_segments"].get(request.attempt_id, 0) + 1
            progress.update(active_subgoal_id=node.id, inflight_execution_id=eid)
            progress["final_verification_ref"] = None
        elif kind == "ExecutionReported":
            if eid not in result.requests:
                continue
            report = ExecutionReport.model_validate(data)
            result.reports[eid] = report
            item = progress["subgoals"][report.subgoal_id]
            if item["execution_ids"][-1] != eid:
                continue
            if report.status != "outcome_unknown":
                item["status"] = "awaiting_verification"
                progress["inflight_execution_id"] = None
            else:
                item["status"] = "running"
                progress["inflight_execution_id"] = eid
        elif kind == "VerificationRequested":
            request = VerificationRequest.model_validate(data)
            result.verification_requests[request.request_id] = request
            continue
        elif kind == "VerificationApplied":
            report = VerificationReport.model_validate(data["report"])
            result.verifications[report.report_id] = report
            result.verification_seq[report.report_id] = event["seq"]
            transition = data["transition"]
            if report.scope == "final_goal":
                progress["final_verification_ref"] = report.report_id
                if transition == "final_pass":
                    progress.update(task_status="succeeded", stop_reason="FINAL_GOALS_VERIFIED")
            else:
                item = progress["subgoals"][report.subject_id]
                item["verification_refs"].append(report.report_id)
                if transition in {"succeeded", "skipped_verified"}:
                    item.update(status=transition, can_continue=False, completed_at=report.checked_at)
                    progress["active_subgoal_id"] = None
                    progress["observations_without_progress"] = 0
                elif transition == "continue":
                    item.update(status="running", can_continue=True)
                    progress["observations_without_progress"] = 0
                elif transition == "needs_recovery":
                    item.update(status="needs_recovery", can_continue=False, last_failure="CONDITIONS_NOT_MET")
                    progress["active_subgoal_id"] = None
                elif transition == "unknown":
                    item.update(status="awaiting_verification", can_continue=False)
                    progress["active_subgoal_id"] = report.subject_id
            if data.get("task_outcome"):
                outcome = data["task_outcome"]
                progress.update(task_status=outcome["status"], stop_reason=outcome["reason"])
        elif kind == "ObservationRequested":
            progress["observations_without_progress"] += 1
            result.last_observation_seq = event["seq"]
        elif kind == "PlanRevised":
            result.plan = TaskPlan.model_validate(data["plan"])
            progress["plan_version"] = result.plan.plan_version
            progress["replans"] += 1
            progress["final_verification_ref"] = None
            for retired in data["retire_subgoals"]:
                progress["subgoals"][retired].update(status="superseded", can_continue=False)
            for node in result.plan.subgoals:
                progress["subgoals"].setdefault(node.id, SubgoalProgress().model_dump())
            progress["active_subgoal_id"] = None
        elif kind == "TaskTerminated":
            progress.update(task_status=data["status"], stop_reason=data["reason"])
        elif kind == "TaskResumed":
            progress.update(task_status="active", stop_reason=None)
        else:
            continue
        progress["revision"] = event["seq"]
    if progress is not None:
        result.progress = TaskProgress.model_validate(progress)
    return result
