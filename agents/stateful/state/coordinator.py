"""Execution → CoF → acknowledged P state → verification/dispatch gates."""

import time

from ..contracts import StateView, digest, utc_now
from ..execution.backend import ExecutionFault
from ..feedback.service import FeedbackService
from .contracts import PStateAck, PUpdateRequest, StateLimits
from .provider import LocalPAdapter, state_signature, validate_ack


class StateCoordinator:
    def __init__(self, manager, analyzer, *, adapter=None, feedback_limits=None, state_limits=None):
        self.manager, self.store = manager, manager.store
        self.adapter = adapter or LocalPAdapter()
        self.limits = state_limits or StateLimits()
        self.feedback = FeedbackService(manager, analyzer, feedback_limits)
        self.store.bind_extension("closed_loop", {"version": "acknowledged-feedback-v1", "p_adapter": self.adapter.name,
                                                  "state_limits": self.limits.model_dump(mode="json")})
        manager.state_guard = self.guard_state
        manager.observation_provider = self.observe_current
        manager.executor.preflight_state_provider = self.preflight
        self.store.feedback_guard = self.guard_dispatch

    def current(self):
        events = [e for e in self.store.events() if e["kind"] == "PUpdateApplied" and not e["payload"]["ack"]["history_only"]]
        return PStateAck.model_validate(events[-1]["payload"]["ack"]) if events else None

    def latest_report(self):
        events = [e for e in self.store.events() if e["kind"] == "ExecutionReported"]
        return events[-1] if events else None

    def require_processed(self, ack):
        applied = {e["payload"]["ack"]["update_id"] for e in self.store.events() if e["kind"] == "PUpdateApplied"}
        if any(e["kind"] == "PUpdateRequested" and e["payload"]["update_id"] not in applied for e in self.store.events()):
            raise ExecutionFault("P_UPDATE_PENDING")
        report = self.latest_report()
        if report and (ack is None or (ack.processed_execution_id, ack.processed_report_revision) !=
                       (report["execution_id"], report["payload"]["report_revision"]) or
                       ack.event_watermark < report["seq"] or not ack.processed_feedback_id or ack.history_only):
            raise ExecutionFault("P_FEEDBACK_PENDING", "P has not acknowledged the latest execution and feedback")
        if report:
            feedback = [e["payload"] for e in self.store.events() if e["kind"] == "CoFFeedbackProduced"
                        and e["execution_id"] == report["execution_id"]
                        and e["payload"]["report_revision"] == report["payload"]["report_revision"]]
            if not feedback or ack.processed_feedback_id != feedback[-1]["feedback_id"]:
                raise ExecutionFault("P_FEEDBACK_PENDING", "latest feedback has not been applied")

    def guard_state(self, state):
        ack = self.current()
        self.require_processed(ack)
        if ack is None or digest(state.model_dump_json()) != digest(ack.state.model_dump_json()):
            raise ExecutionFault("P_STATE_STALE", "use the latest acknowledged P snapshot")

    def guard_dispatch(self, request):
        ack = self.current()
        self.require_processed(ack)
        if ack is None or request.based_on_state_version != ack.state.state_version:
            raise ExecutionFault("P_STATE_STALE")

    def preflight(self, observation):
        ack = self.current()
        self.require_processed(ack)
        if ack is None or observation.state is None or observation.state.state_version != ack.source_state_version:
            raise ExecutionFault("STATE_STALE", "physical observation changed since P snapshot")
        latest = [e for e in self.store.events() if e["kind"] == "PUpdateApplied"
                  and e["payload"]["ack"]["snapshot_id"] == ack.snapshot_id][-1]["payload"]["request"]
        raw = StateView.model_validate(latest["observation"]) if latest["observation"] else None
        if state_signature(observation.state) != state_signature(raw):
            raise ExecutionFault("STATE_STALE", "physical facts changed since P snapshot")
        return ack.state

    def update(self, request):
        requests = [e["payload"] for e in self.store.events() if e["kind"] == "PUpdateRequested"
                    and e["payload"]["update_id"] == request.update_id]
        if requests and requests[0] != request.model_dump(mode="json"):
            raise ExecutionFault("P_UPDATE_ID_CONFLICT")
        applied = [e["payload"] for e in self.store.events() if e["kind"] == "PUpdateApplied"
                   and e["payload"]["ack"]["update_id"] == request.update_id]
        if applied:
            return PStateAck.model_validate(applied[0]["ack"])
        previous = self.current()
        if request.previous_snapshot_id != (previous.snapshot_id if previous else None):
            raise ExecutionFault("P_UPDATE_STALE")
        if not requests:
            if sum(e["kind"] == "PUpdateRequested" for e in self.store.events()) >= self.limits.max_updates:
                raise ExecutionFault("P_UPDATE_BUDGET_EXHAUSTED")
            self.store.append("PUpdateRequested", request.execution_id or request.episode_id, request.model_dump(mode="json"))
        start = time.monotonic()
        attempts = sum(e["kind"] == "PUpdateAttempted" and e["payload"]["update_id"] == request.update_id
                       for e in self.store.events())
        for index in range(attempts, self.limits.max_update_attempts):
            remaining = self.limits.update_timeout_s - (time.monotonic() - start)
            if remaining <= 0:
                break
            self.store.append("PUpdateAttempted", request.execution_id or request.episode_id,
                              {"update_id": request.update_id, "attempt": index + 1})
            try:
                ack = self.adapter.update(request, previous, timeout_s=remaining)
                if ack is None:
                    continue
                if time.monotonic() - start > self.limits.update_timeout_s:
                    break
                ack = PStateAck.model_validate_json(ack.model_dump_json())
                validate_ack(ack, request, previous)
                payload = {"request": request.model_dump(mode="json"), "ack": ack.model_dump(mode="json")}
                self.store.append("PUpdateApplied", request.execution_id or request.episode_id, payload)
                self.store.write_json(f"state/{request.update_id}.json", payload)
                return ack
            except Exception as exc:
                self.store.append("PUpdateRejected", request.execution_id or request.episode_id,
                                  {"update_id": request.update_id, "reason": getattr(exc, "reason", type(exc).__name__)})
        raise ExecutionFault("P_ACK_TIMEOUT", "P did not return a valid processing acknowledgement within its budget")

    def _request(self, observation, *, cof_request=None, feedback=None):
        previous = self.current()
        applied_ids = {e["payload"]["ack"]["update_id"] for e in self.store.events() if e["kind"] == "PUpdateApplied"}
        pending = [e["payload"] for e in self.store.events() if e["kind"] == "PUpdateRequested"
                   and e["payload"]["update_id"] not in applied_ids]
        if pending:
            # Reuse the persisted input and its remaining attempts after an interrupted handoff.
            old = PUpdateRequest.model_validate(pending[-1])
            if feedback and old.feedback_id != feedback.feedback_id:
                raise ExecutionFault("P_UPDATE_PENDING")
            return old
        index = 1 + sum(e["kind"] == "PUpdateRequested" for e in self.store.events())
        lineage = {}
        if cof_request:
            for e in cof_request.evidence:
                lineage[e.evidence_id] = sorted({r for f in e.state.facts for r in f.evidence_refs}) if e.state else [f"frame:{e.sha256}"]
        return PUpdateRequest(update_id=f"p-update-{index}", episode_id=self.manager.task.episode_id,
                    previous_snapshot_id=previous.snapshot_id if previous else None,
                    event_watermark=feedback.event_watermark if feedback else previous.event_watermark if previous else 0,
                    execution_id=feedback.execution_id if feedback else previous.processed_execution_id if previous else None,
                    report_revision=feedback.report_revision if feedback else previous.processed_report_revision if previous else None,
                    feedback_id=feedback.feedback_id if feedback else previous.processed_feedback_id if previous else None,
                    observation=observation, feedback=feedback, evidence_lineage=lineage, source=self.manager.source,
                    observed_at=observation.observed_at if observation else utc_now())

    def observe_current(self):
        self.require_processed(self.current())
        if self.store.pending() or self.manager.executor.backend.motion_state() != "idle":
            raise ExecutionFault("EXECUTION_UNKNOWN")
        observation = self.manager.executor.backend.observe()
        request = self._request(observation.state)
        return self.update(request).state

    def after_execution(self, report):
        cof_request, feedback = self.feedback.analyze(report)
        current = self.current()
        if current and current.processed_feedback_id == feedback.feedback_id:
            return current.state
        if any(e["kind"] == "PUpdateApplied" and e["payload"]["ack"]["processed_feedback_id"] == feedback.feedback_id
               for e in self.store.events()):
            return current.state
        ends = [e.state for e in cof_request.evidence if e.state is not None and e.boundary in {"after_execution", "reconcile"}]
        observation = max(ends, key=lambda state: state.observed_at) if ends else None
        request = self._request(observation, cof_request=cof_request, feedback=feedback)
        state = self.update(request).state
        if "COF_ANALYSIS_BUDGET_EXHAUSTED" in feedback.uncertainties:
            raise ExecutionFault("COF_ANALYSIS_BUDGET_EXHAUSTED")
        return state
