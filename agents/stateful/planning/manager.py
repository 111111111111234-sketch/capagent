"""Plan selection, verified progress and local revision for a single episode.

The manager owns no parallel progress database. Dispatches, reports, checks and
patches share EventStore transactions, and progress is rebuilt from those events.
"""

from __future__ import annotations

import uuid

from ..contracts import Condition, ExecutionBudget, ExecutionRequest, StateView
from ..execution.backend import ExecutionFault
from ..execution.executor import Executor
from ..execution.validation import check_time, validate_request, validate_state
from .contracts import (
    PlanPatch, PlanProposal, PlannerDecision, PlanningLimits, PredicateCatalog, SegmentContract,
    TaskPlan, TaskProgress, VerificationReport, VerificationRequest,
)
from .progress import PlanningState, restore
from .validation import purpose_key, validate_conditions, validate_plan
from .verification import aggregate, condition_checks, verify

DONE = {"succeeded", "skipped_verified"}


class PlanningManager:
    def __init__(self, executor: Executor, predicates: PredicateCatalog,
                 limits: PlanningLimits | None = None):
        self.executor, self.store, self.task = executor, executor.store, executor.task
        if restore(self.store).plan is None and self.store.usage()["executions"]:
            raise ExecutionFault("PLAN_ALREADY_STARTED", "planning requires a fresh episode; existing execution runs remain unchanged")
        self.predicates = PredicateCatalog.model_validate_json(predicates.model_dump_json())
        self.limits = limits or PlanningLimits()
        self.source = "mock" if executor.backend.profile.mode == "mock" else "observed"
        self.store.bind_extension("planning", {"limits": self.limits.model_dump(mode="json"),
                                               "predicates": self.predicates.model_dump(mode="json")})
        self._authorized: dict[str, SegmentContract] = {}
        self.state_guard = None
        self.observation_provider = None
        self.store.dispatch_guard = self._guard_dispatch

    def read(self) -> PlanningState:
        state = restore(self.store)
        if state.plan is None:
            raise ExecutionFault("NO_PLAN")
        return state

    def create_plan(self, proposal: PlanProposal, state: StateView) -> TaskPlan:
        if restore(self.store).plan is not None or self.store.usage()["executions"]:
            raise ExecutionFault("PLAN_ALREADY_STARTED", "initial planning requires a fresh episode")
        self._check_state(state)
        validate_plan(proposal, self.task, state, self.executor.catalog, self.predicates,
                      self.limits.max_subgoals)
        if any(node.recovery_of is not None for node in proposal.subgoals):
            raise ExecutionFault("INVALID_PLAN", "initial nodes cannot refer to a recovery history")
        plan = TaskPlan(**proposal.model_dump(), episode_id=self.task.episode_id,
                        task_id=self.task.task_id, task_version=self.task.task_version, plan_version=1)
        self.store.append("PlanCreated", self.task.episode_id, {"plan": plan.model_dump(mode="json")})
        self.export()
        return plan

    def _check_state(self, state: StateView) -> None:
        if self.store.db.execute("SELECT 1 FROM metadata WHERE key='closed_loop'").fetchone():
            if self.state_guard is None:
                raise ExecutionFault("FEEDBACK_GUARD_REQUIRED")
            self.state_guard(state)
        if state.episode_id != self.task.episode_id:
            raise ExecutionFault("STATE_EPISODE_MISMATCH")
        check_time(state.observed_at, self.limits.max_state_age_s)

    def _conditions(self, conditions: list[Condition], state: StateView) -> str:
        return aggregate(condition_checks(conditions, state, max_age=self.limits.max_state_age_s,
                                          source=self.source))

    def next(self, state: StateView | None) -> PlannerDecision:
        current = self.read()
        plan, progress = current.plan, current.progress
        if progress.task_status != "active":
            return PlannerDecision(kind="stopped", reason=progress.stop_reason or progress.task_status)
        if self.store.pending() or progress.inflight_execution_id:
            return PlannerDecision(kind="wait_for_execution", reason="execution must be reconciled")
        if state is None:
            return PlannerDecision(kind="request_observation", reason="state is unavailable")
        try:
            self._check_state(state)
        except ExecutionFault:
            return PlannerDecision(kind="request_observation", reason="state is stale or mismatched")
        # A returned program never skips the independent check of its current subgoal.
        for node in plan.subgoals:
            item = progress.subgoals[node.id]
            if item.status == "awaiting_verification":
                if item.verification_refs:
                    last = item.verification_refs[-1]
                    report = current.verifications[last]
                    if (aggregate(report.groups["subgoal"]) == "unknown"
                            and report.execution_id == (item.execution_ids[-1] if item.execution_ids else None)
                            and current.last_observation_seq <= current.verification_seq[last]):
                        return PlannerDecision(kind="request_observation", subgoal_id=node.id,
                                               reason="last verification lacked evidence")
                return PlannerDecision(kind="verify_subgoal", subgoal_id=node.id, reason="execution returned")
            if item.can_continue:
                segment = current.segments[item.execution_ids[-1]]
                result = self._conditions(segment.continue_conditions, state)
                if result != "pass":
                    return PlannerDecision(kind="request_observation" if result == "unknown" else "revise_plan",
                                           subgoal_id=node.id, reason="continuation conditions changed")
                if progress.attempt_segments[item.active_attempt_id] >= self.limits.max_segments_per_attempt:
                    return self._exhausted("segment budget exhausted within the current attempt")
                return PlannerDecision(kind="continue_subgoal", subgoal_id=node.id, reason="verified segment progress")
        final_result = self._conditions(self.task.goal_conditions, state)
        if final_result == "pass":
            return PlannerDecision(kind="verify_final", reason="final goals may already hold")
        active = [node for node in plan.subgoals if progress.subgoals[node.id].status != "superseded"]
        if all(progress.subgoals[node.id].status in DONE for node in active):
            previous = progress.final_verification_ref
            if previous:
                result = aggregate(current.verifications[previous].groups["final_goal"])
                if result == "fail":
                    return PlannerDecision(kind="revise_plan", reason="final goals no longer hold")
                if result == "unknown" and current.last_observation_seq <= current.verification_seq[previous]:
                    return PlannerDecision(kind="request_observation", reason="final verification needs evidence")
            return PlannerDecision(kind="verify_final", reason="all historical steps finished; check current goals")
        usage = self.store.usage()
        if usage["executions"] >= self.task.budget.max_executions or usage["api_calls"] >= self.task.budget.max_api_calls:
            return self._exhausted("episode execution/API budget exhausted")
        unknown = False
        for node in active:
            item = progress.subgoals[node.id]
            if item.status not in {"pending", "needs_recovery", "blocked"}:
                continue
            if not all(progress.subgoals[dep].status in DONE for dep in node.depends_on):
                continue
            if self._conditions(node.success_conditions, state) == "pass":
                return PlannerDecision(kind="verify_subgoal", subgoal_id=node.id,
                                       reason="goal may be satisfied without an action")
            result = self._conditions(node.preconditions, state)
            if result == "unknown":
                unknown = True
            elif result == "pass":
                if progress.recovery_attempts.get(node.recovery_group_id, 0) >= self.limits.max_attempts_per_recovery_group:
                    return self._exhausted("recovery group attempt budget exhausted")
                return PlannerDecision(kind="execute_subgoal", subgoal_id=node.id,
                                       reason="retry with updated strategy" if item.attempt_count else "ready subgoal")
        return PlannerDecision(kind="request_observation" if unknown else "revise_plan",
                               reason="missing evidence" if unknown else "no unfinished subgoal is currently executable")

    def _exhausted(self, reason: str) -> PlannerDecision:
        self.terminate("budget_exhausted", reason)
        return PlannerDecision(kind="stopped", reason=reason)

    def prepare_segment(self, subgoal_id: str, state: StateView, *, code: str, inputs: dict | None = None,
                        entry_conditions: list[Condition] | None = None,
                        expected_conditions: list[Condition] | None = None,
                        continue_conditions: list[Condition] | None = None,
                        budget: ExecutionBudget | None = None) -> tuple[ExecutionRequest, SegmentContract]:
        """Validate a proposal without authorizing or dispatching any action."""
        decision = self.next(state)
        if decision.kind not in {"execute_subgoal", "continue_subgoal"} or decision.subgoal_id != subgoal_id:
            raise ExecutionFault("SUBGOAL_NOT_READY", decision.reason)
        current = self.read()
        node = next(node for node in current.plan.subgoals if node.id == subgoal_id)
        item = current.progress.subgoals[subgoal_id]
        index = self.store.usage()["executions"] + 1
        attempt_id = item.active_attempt_id if item.can_continue else f"attempt-{index}"
        entry = entry_conditions if entry_conditions is not None else node.preconditions
        expected = expected_conditions if expected_conditions is not None else node.success_conditions
        continuation = continue_conditions or []
        definitions = {c.id: c for n in current.plan.subgoals for c in n.preconditions + n.success_conditions}
        validate_conditions(entry + expected + continuation, state, self.predicates, definitions)
        if not item.can_continue and not all(condition in entry for condition in node.preconditions):
            raise ExecutionFault("PRECONDITIONS_WEAKENED")
        if self._conditions(entry, state) != "pass":
            raise ExecutionFault("SEGMENT_NOT_READY")
        segment = SegmentContract(segment_id=f"segment-{index}", subgoal_id=subgoal_id,
                                  attempt_id=attempt_id, entry_conditions=entry,
                                  expected_conditions=expected, continue_conditions=continuation,
                                  budget=budget or ExecutionBudget())
        request = ExecutionRequest(
            episode_id=self.task.episode_id, execution_id=f"exec-{index}", task_version=self.task.task_version,
            plan_version=current.plan.plan_version, subgoal_id=subgoal_id, attempt_id=attempt_id,
            segment_id=segment.segment_id, catalog_version=self.executor.catalog.catalog_version,
            based_on_state_version=state.state_version, code=code, inputs=inputs or {},
            allowed_skills=node.candidate_skills, entry_conditions=entry,
            max_state_age_s=self.limits.max_state_age_s, budget=segment.budget,
        )
        validate_request(request, self.task, self.executor.catalog)
        validate_state(request, state)
        return request, segment

    def execute_segment(self, subgoal_id: str, state: StateView, **kwargs):
        request, segment = self.prepare_segment(subgoal_id, state, **kwargs)
        self._authorized[request.request_hash] = segment
        try:
            return self.executor.execute(request)
        finally:
            self._authorized.pop(request.request_hash, None)
            self.export()

    def _guard_dispatch(self, request: ExecutionRequest) -> dict:
        segment = self._authorized.get(request.request_hash)
        current = self.read()
        if segment is None or current.progress.task_status != "active" or request.plan_version != current.plan.plan_version:
            raise ExecutionFault("UNAUTHORIZED_PLAN_DISPATCH")
        node = next(node for node in current.plan.subgoals if node.id == request.subgoal_id)
        item = current.progress.subgoals[node.id]
        if current.progress.inflight_execution_id or item.status not in {"pending", "needs_recovery", "blocked", "running"}:
            raise ExecutionFault("SUBGOAL_NOT_READY")
        if item.can_continue:
            if current.progress.attempt_segments[request.attempt_id] >= self.limits.max_segments_per_attempt:
                raise ExecutionFault("BUDGET_EXCEEDED", "attempt segment limit")
        elif current.progress.recovery_attempts.get(node.recovery_group_id, 0) >= self.limits.max_attempts_per_recovery_group:
            raise ExecutionFault("BUDGET_EXCEEDED", "recovery group attempt limit")
        return segment.model_dump(mode="json")

    def request_verification(self, subgoal_id: str | None = None) -> VerificationRequest:
        current = self.read()
        self._require_active_idle(current)
        if subgoal_id is None and any(item.status == "awaiting_verification" for item in current.progress.subgoals.values()):
            raise ExecutionFault("SUBGOAL_VERIFICATION_REQUIRED")
        groups = {"final_goal": self.task.goal_conditions}
        execution_id = next(reversed(current.requests), None)
        if subgoal_id is not None:
            node = next((n for n in current.plan.subgoals if n.id == subgoal_id), None)
            if node is None or current.progress.subgoals[node.id].status not in {
                "pending", "needs_recovery", "blocked", "awaiting_verification",
            }:
                raise ExecutionFault("VERIFICATION_SUBJECT_NOT_READY")
            item = current.progress.subgoals[subgoal_id]
            execution_id = item.execution_ids[-1] if item.execution_ids else None
            groups = {"subgoal": node.success_conditions}
            if execution_id:
                if execution_id != next(reversed(current.requests)):
                    raise ExecutionFault("VERIFICATION_STALE_SUBJECT", "cannot attribute later effects to an old execution")
                segment = current.segments[execution_id]
                groups.update(segment_expected=segment.expected_conditions,
                              segment_continue=segment.continue_conditions)
        # Verify against the latest known stop, including actions of other subgoals.
        stop_events = [event for event in self.store.events() if event["kind"] == "BackendStopConfirmed"]
        min_version = max((request.based_on_state_version for request in current.requests.values()), default=0)
        for report in current.reports.values():
            if report.state_after_ref:
                observed = StateView.model_validate_json((self.store.directory / report.state_after_ref).read_text())
                min_version = max(min_version, observed.state_version)
        request = VerificationRequest(
            request_id=f"check-{uuid.uuid4()}", episode_id=self.task.episode_id,
            plan_version=current.plan.plan_version, scope="subgoal" if subgoal_id else "final_goal",
            subject_id=subgoal_id or self.task.task_id, execution_id=execution_id, groups=groups,
            not_before=stop_events[-1]["timestamp"] if stop_events else None, min_state_version=min_version,
        )
        self.store.append("VerificationRequested", execution_id or self.task.episode_id, request.model_dump(mode="json"))
        return request

    def apply_verification(self, report: VerificationReport, state: StateView) -> TaskProgress:
        current = self.read()
        previous = current.verifications.get(report.report_id)
        if previous is not None:
            if previous != report:
                raise ExecutionFault("VERIFICATION_ID_CONFLICT")
            return current.progress
        self._require_active_idle(current)
        self._check_state(state)
        request = current.verification_requests.get(report.request_id)
        if request is None or report.plan_version != current.plan.plan_version:
            raise ExecutionFault("VERIFICATION_REQUEST_MISMATCH")
        if any(item.request_id == report.request_id for item in current.verifications.values()):
            raise ExecutionFault("VERIFICATION_ALREADY_APPLIED")
        # No newly dispatched action may intervene between requesting and applying a check.
        events = self.store.events()
        requested_seq = next(e["seq"] for e in events if e["kind"] == "VerificationRequested"
                             and e["payload"]["request_id"] == report.request_id)
        if any(e["kind"] == "ExecutionDispatched" and e["seq"] > requested_seq for e in events):
            raise ExecutionFault("VERIFICATION_STALE")
        expected = verify(request, state, max_age=self.limits.max_state_age_s,
                          source=self.source, report_id=report.report_id)
        actual_data, expected_data = report.model_dump(), expected.model_dump()
        actual_data.pop("checked_at")
        expected_data.pop("checked_at")
        check_time(report.checked_at, self.limits.max_state_age_s)
        if actual_data != expected_data:
            raise ExecutionFault("VERIFICATION_EVIDENCE_MISMATCH")
        if report.scope == "final_goal":
            transition = "final_" + aggregate(report.groups["final_goal"])
        else:
            item = current.progress.subgoals[report.subject_id]
            verdict = aggregate(report.groups["subgoal"])
            if verdict == "pass":
                transition = "succeeded" if item.execution_ids else "skipped_verified"
            elif verdict == "unknown":
                transition = "unknown"
            elif (item.execution_ids and current.reports[item.execution_ids[-1]].status == "completed"
                  and aggregate(report.groups["segment_expected"]) == "pass"
                  and report.groups["segment_continue"]
                  and aggregate(report.groups["segment_continue"]) == "pass"):
                transition = "continue"
            else:
                transition = "needs_recovery"
        task_outcome = None
        if report.scope == "subgoal" and report.execution_id:
            execution = current.reports[report.execution_id]
            if execution.status == "cancelled":
                task_outcome = {"status": "interrupted", "reason": "execution cancelled"}
            elif execution.status == "timed_out" or execution.stop_reason == "BUDGET_EXCEEDED":
                task_outcome = {"status": "budget_exhausted", "reason": execution.stop_reason}
            elif execution.status == "error":
                task_outcome = {"status": "blocked", "reason": "execution error requires explicit recovery review"}
        snapshot = self.store.write_json(f"planning/evidence/{report.report_id}.json", state.model_dump(mode="json"))
        # Verification and the cancellation/error policy are one atomic event so
        # a crash cannot retain success history while losing the required stop.
        self.store.append("VerificationApplied", report.execution_id or self.task.episode_id,
                          {"report": report.model_dump(mode="json"), "transition": transition,
                           "state_ref": snapshot, "task_outcome": task_outcome})
        self.export()
        return self.read().progress

    def verify_now(self, state: StateView, subgoal_id: str | None = None) -> TaskProgress:
        self._check_state(state)
        request = self.request_verification(subgoal_id)
        report = verify(request, state, max_age=self.limits.max_state_age_s, source=self.source)
        return self.apply_verification(report, state)

    def observe(self) -> StateView | None:
        current = self.read()
        self._require_active_idle(current)
        if current.progress.observations_without_progress >= self.limits.max_observations_without_progress:
            self.terminate("budget_exhausted", "observation requests made no verified progress")
            return None
        self.store.append("ObservationRequested", self.task.episode_id, {"reason": "missing or stale evidence"})
        try:
            state = self.observation_provider() if self.observation_provider else self.executor.backend.observe().state
            if state is not None:
                self._check_state(state)
                self.store.write_json(f"planning/observations/{self.read().progress.revision}.json", state.model_dump(mode="json"))
            return state
        except Exception as exc:
            self.store.append("PlanningObservationFailed", self.task.episode_id, {"error": str(exc)})
            return None
        finally:
            self.export()

    def commit_patch(self, patch: PlanPatch, state: StateView) -> TaskPlan:
        current = self.read()
        self._require_active_idle(current)
        self._check_state(state)
        plan, progress = current.plan, current.progress
        if (patch.base_plan_version, patch.base_progress_revision) != (plan.plan_version, progress.revision):
            raise ExecutionFault("STALE_PLAN_PATCH")
        if any(item.status == "awaiting_verification" for item in progress.subgoals.values()):
            raise ExecutionFault("VERIFY_BEFORE_REVISION")
        if progress.replans >= self.limits.max_replans:
            self.terminate("budget_exhausted", "plan revision budget exhausted")
            raise ExecutionFault("BUDGET_EXCEEDED")
        nodes = {node.id: node for node in plan.subgoals}
        retired = {node for node, item in progress.subgoals.items() if item.status == "superseded"}
        if len(patch.retire_subgoals) != len(set(patch.retire_subgoals)):
            raise ExecutionFault("INVALID_PATCH", "duplicate retirement")
        for node_id in patch.retire_subgoals:
            if node_id not in nodes or progress.subgoals[node_id].status in DONE | {"superseded"}:
                raise ExecutionFault("HISTORY_IMMUTABLE", node_id)
            retired.add(node_id)
        for node in patch.add_subgoals:
            if node.id in nodes or node.recovery_of not in {old.id for old in plan.subgoals}:
                raise ExecutionFault("INVALID_PATCH", "new recovery nodes must refer to an existing purpose")
            if node.recovery_group_id != nodes[node.recovery_of].recovery_group_id:
                raise ExecutionFault("RECOVERY_BUDGET_RESET", "a recovery node must inherit its original group")
            original = nodes[node.recovery_of]
            if (purpose_key(node.success_conditions) != purpose_key(original.success_conditions)
                    or not set(node.candidate_skills) <= set(original.candidate_skills)
                    or not all(condition in node.preconditions for condition in original.preconditions)):
                raise ExecutionFault("RECOVERY_CONTRACT_CHANGED", "this slice restores an existing purpose without weakening its contract")
            nodes[node.id] = node
        for node_id, dependencies in patch.dependency_updates.items():
            if node_id not in nodes or (node_id in progress.subgoals and progress.subgoals[node_id].status in DONE | {"superseded"}):
                raise ExecutionFault("HISTORY_IMMUTABLE", node_id)
            nodes[node_id] = nodes[node_id].model_copy(update={"depends_on": dependencies})
        revised = TaskPlan(
            episode_id=plan.episode_id, task_id=plan.task_id, task_version=plan.task_version,
            plan_version=plan.plan_version + 1, based_on_state_version=patch.based_on_state_version,
            subgoals=list(nodes.values()), goal_coverage=patch.goal_coverage if patch.goal_coverage is not None else plan.goal_coverage,
        )
        if not patch.add_subgoals and not patch.retire_subgoals and revised.subgoals == plan.subgoals and revised.goal_coverage == plan.goal_coverage:
            raise ExecutionFault("EMPTY_PATCH")
        validate_plan(revised, self.task, state, self.executor.catalog, self.predicates,
                      self.limits.max_subgoals, retired)
        self.store.append("PlanRevised", self.task.episode_id,
                          {"plan": revised.model_dump(mode="json"), "patch": patch.model_dump(mode="json"),
                           "retire_subgoals": patch.retire_subgoals})
        self.export()
        return revised

    def _require_active_idle(self, current: PlanningState) -> None:
        if current.progress.task_status != "active":
            raise ExecutionFault("TASK_NOT_ACTIVE")
        if self.store.pending() or current.progress.inflight_execution_id or self.executor.backend.motion_state() != "idle":
            raise ExecutionFault("EXECUTION_UNKNOWN", "wait for execution or reconcile first")

    def terminate(self, status: str, reason: str) -> None:
        if status not in {"failed", "blocked", "budget_exhausted", "interrupted"}:
            raise ExecutionFault("INVALID_TERMINATION", "success requires final verification")
        current = self.read()
        if current.progress.task_status != "active":
            return
        self.store.append("TaskTerminated", self.task.episode_id, {"status": status, "reason": reason})
        self.export()

    def resume(self, state: StateView) -> None:
        current = self.read()
        if current.progress.task_status not in {"interrupted", "blocked"}:
            raise ExecutionFault("TASK_NOT_RESUMABLE")
        if self.store.pending() or self.executor.backend.motion_state() != "idle":
            raise ExecutionFault("EXECUTION_UNKNOWN")
        self._check_state(state)
        self.store.append("TaskResumed", self.task.episode_id, {"state_version": state.state_version})
        self.export()

    def export(self) -> None:
        current = restore(self.store)
        if current.plan is not None:
            self.store.write_json(f"planning/plan-v{current.plan.plan_version}.json", current.plan.model_dump(mode="json"))
            self.store.write_json("planning/progress.json", current.progress.model_dump(mode="json"))
