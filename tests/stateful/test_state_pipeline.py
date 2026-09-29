"""P handoff, dispatch gates and complete closed-loop behavior tests."""

import tempfile
import time
import unittest
from pathlib import Path

from capx.agents.stateful.contracts import StateView
from capx.agents.stateful.execution.backend import ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore, replay
from capx.agents.stateful.feedback.analyzers import SensorTimelineAnalyzer
from capx.agents.stateful.feedback.contracts import AnalysisQuery, CoFProposal, EvidenceClaim, FeedbackLimits
from capx.agents.stateful.feedback.demo import run_closed_loop
from capx.agents.stateful.planning.fixtures import FixedPlanner, HOLDING, StackingBackend, predicates, stack_task
from capx.agents.stateful.planning.manager import PlanningManager
from capx.agents.stateful.models.client import ModelClient
from capx.agents.stateful.models.contracts import ModelConfig
from capx.agents.stateful.models.fixtures import ScriptedTransport
from capx.agents.stateful.models.generator import ModelGenerator
from capx.agents.stateful.models.runner import fixture_inputs, validate_fixture_segment
from capx.agents.stateful.state.contracts import PStateAck, StateLimits
from capx.agents.stateful.state.coordinator import StateCoordinator
from capx.agents.stateful.state.provider import LocalPAdapter, merge_state, validate_ack


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EventStore(self.root)
        self.task = stack_task("p-test")
        self.backend = StackingBackend(self.task.episode_id)
        self.executor = Executor(self.task, self.backend, self.store, execution_mode="process")
        self.manager = PlanningManager(self.executor, predicates())
        self.pipeline = StateCoordinator(self.manager, SensorTimelineAnalyzer())
        self.state = self.pipeline.observe_current()
        self.manager.create_plan(FixedPlanner().create_plan(self.state), self.state)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def execute(self, node_id="grasp", state=None):
        current = self.manager.read()
        node = next(n for n in current.plan.subgoals if n.id == node_id)
        code, inputs = FixedPlanner().code_for(node, current.progress.subgoals[node_id].attempt_count)
        return self.manager.execute_segment(node_id, state or self.pipeline.current().state, code=code, inputs=inputs)

    def test_handoff_order_and_lineage_are_durable_before_verification(self):
        report = self.execute()
        state = self.pipeline.after_execution(report)
        ack = self.pipeline.current()
        self.assertEqual(ack.processed_execution_id, report.execution_id)
        self.assertTrue(ack.processed_feedback_id)
        holding = next(f for f in state.facts if f.predicate == "holding")
        self.assertEqual(len(holding.evidence_refs), 1)  # CoF and raw sensor share the same root.
        self.manager.verify_now(state, "grasp")
        kinds = [e["kind"] for e in self.store.events()]
        self.assertLess(kinds.index("ExecutionReported"), kinds.index("CoFFeedbackProduced"))
        last_p = max(i for i, k in enumerate(kinds) if k == "PUpdateApplied")
        self.assertLess(kinds.index("CoFFeedbackProduced"), last_p)
        self.assertLess(last_p, kinds.index("VerificationApplied"))

    def test_raw_state_cannot_bypass_unprocessed_feedback(self):
        self.execute()
        with self.assertRaisesRegex(ExecutionFault, "P has not acknowledged"):
            self.manager.verify_now(self.backend.observe().state, "grasp")
        with self.assertRaises(ExecutionFault):
            self.pipeline.observe_current()

    def test_higher_state_version_without_processed_ids_is_rejected(self):
        report = self.execute()
        request, feedback = self.pipeline.feedback.analyze(report)
        observation = next(e.state for e in request.evidence if e.state and e.boundary == "after_execution")
        update = self.pipeline._request(observation, cof_request=request, feedback=feedback)
        previous = self.pipeline.current()
        ack = merge_state(update, previous)
        forged = ack.model_copy(update={"processed_feedback_id": None,
                                       "state": ack.state.model_copy(update={"state_version": 9999})})
        with self.assertRaises(ExecutionFault):
            validate_ack(forged, update, previous)

    def test_p_cannot_promote_unsupported_sensor_fact(self):
        request = self.pipeline._request(self.backend.observe().state)
        previous = self.pipeline.current()
        ack = merge_state(request, previous)
        facts = [f.model_copy(update={"value": True}) if f.predicate == "holding" else f for f in ack.state.facts]
        forged = ack.model_copy(update={"state": ack.state.model_copy(update={"facts": facts})})
        with self.assertRaisesRegex(ExecutionFault, "P_STATE_EVIDENCE_MISMATCH"):
            validate_ack(forged, request, previous)

    def test_duplicate_update_is_idempotent_and_conflicting_id_rejected(self):
        request = self.pipeline._request(self.backend.observe().state)
        first = self.pipeline.update(request)
        count = len(self.store.events())
        self.assertEqual(self.pipeline.update(request), first)
        self.assertEqual(len(self.store.events()), count)
        with self.assertRaisesRegex(ExecutionFault, "P_UPDATE_ID_CONFLICT"):
            self.pipeline.update(request.model_copy(update={"event_watermark": 99}))

    def test_duplicate_execution_feedback_does_not_repeat_fusion(self):
        report = self.execute()
        first = self.pipeline.after_execution(report)
        count = len(self.store.events())
        second = self.pipeline.after_execution(report)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.events()), count)

    def test_conflicting_visual_candidate_yields_unknown_then_fresh_observation(self):
        class ConflictAnalyzer(SensorTimelineAnalyzer):
            def analyze(self, request, error=None):
                base = super().analyze(request)
                frame = next(e for e in request.evidence if e.kind == "frame" and e.boundary == "after_execution")
                candidate = EvidenceClaim(claim_id="injected-visual-conflict", predicate="holding", args=HOLDING.args,
                                          value=False, observed_at=frame.observed_at, evidence_refs=[frame.evidence_id])
                return CoFProposal(claims=base.claims + [candidate])
        self.pipeline.feedback.analyzer = ConflictAnalyzer()
        state = self.pipeline.after_execution(self.execute())
        self.assertIsNone(next(f.value for f in state.facts if f.predicate == "holding"))
        self.assertTrue(self.pipeline.current().conflicts)
        self.manager.verify_now(state, "grasp")
        self.assertEqual(self.manager.next(state).kind, "request_observation")
        state = self.manager.observe()
        self.manager.verify_now(state, "grasp")
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "succeeded")

    def test_failed_execution_still_updates_physical_changes(self):
        self.backend.scenario = "partial_error"
        report = self.execute()
        self.assertEqual(report.status, "error")
        state = self.pipeline.after_execution(report)
        self.assertTrue(next(f.value for f in state.facts if f.predicate == "lifted"))
        self.manager.verify_now(state, "grasp")
        progress = self.manager.read().progress
        self.assertEqual(progress.task_status, "blocked")
        self.assertEqual(progress.subgoals["grasp"].status, "succeeded")

    def test_unknown_execution_cannot_be_unlocked_by_p(self):
        self.backend.scenario, self.backend.stop_available = "stop_unknown", False
        report = self.execute()
        state = self.pipeline.after_execution(report)
        self.assertEqual(self.pipeline.current().processed_execution_id, report.execution_id)
        self.assertEqual(self.manager.next(state).kind, "wait_for_execution")
        with self.assertRaises(ExecutionFault):
            self.pipeline.observe_current()

    def test_latest_feedback_not_only_latest_execution_must_be_acknowledged(self):
        report = self.execute()
        state = self.pipeline.after_execution(report)
        self.pipeline.feedback.analyze(report, [AnalysisQuery(query_id="extra-query", condition=HOLDING, scope="occurred")])
        with self.assertRaisesRegex(ExecutionFault, "latest feedback"):
            self.manager.verify_now(state, "grasp")

    def test_physical_state_change_at_preflight_prevents_dispatch(self):
        self.backend.version += 1
        with self.assertRaisesRegex(ExecutionFault, "physical observation changed"):
            self.execute()
        self.assertEqual(self.store.usage()["executions"], 0)
        self.assertEqual(self.backend.calls, [])

    def test_old_p_snapshot_is_rejected_even_with_same_version(self):
        old = self.state
        self.pipeline.observe_current()
        with self.assertRaisesRegex(ExecutionFault, "latest acknowledged"):
            self.pipeline.guard_state(old)

    def test_missing_observation_invalidates_old_certainty(self):
        state = self.pipeline.after_execution(self.execute())
        request = self.pipeline._request(None)
        ack = self.pipeline.update(request)
        self.assertTrue(next(f.value for f in state.facts if f.predicate == "holding"))
        self.assertIsNone(next(f.value for f in ack.state.facts if f.predicate == "holding"))

    def test_late_feedback_is_history_only_and_cannot_overwrite_current_state(self):
        first_report = self.execute()
        state = self.pipeline.after_execution(first_report)
        self.manager.verify_now(state, "grasp")
        second_report = self.execute("transport", state)
        self.pipeline.after_execution(second_report)
        current = self.pipeline.current()
        request, feedback = self.pipeline.feedback.analyze(first_report)
        raw = next(e.state for e in request.evidence if e.state and e.boundary == "after_execution")
        update = self.pipeline._request(raw, cof_request=request, feedback=feedback)
        ack = self.pipeline.update(update)
        self.assertTrue(ack.history_only)
        self.assertEqual(self.pipeline.current(), current)
        self.assertEqual(replay(self.root)["p_state"]["snapshot_id"], current.snapshot_id)

    def test_p_retry_limit_cannot_reset_by_reissuing_handoff(self):
        class Unavailable(LocalPAdapter):
            def update(self, request, previous, *, timeout_s):
                return None
        self.pipeline.adapter = Unavailable()
        report = self.execute()
        for _ in range(2):
            with self.assertRaisesRegex(ExecutionFault, "P did not return"):
                self.pipeline.after_execution(report)
        pending = [e for e in self.store.events() if e["kind"] == "PUpdateRequested" and e["payload"]["feedback_id"]]
        self.assertEqual(len(pending), 1)
        attempts = [e for e in self.store.events() if e["kind"] == "PUpdateAttempted"
                    and e["payload"]["update_id"] == pending[0]["payload"]["update_id"]]
        self.assertEqual(len(attempts), 3)

    def test_restart_restores_p_and_requires_feedback_guard(self):
        report = self.execute()
        state = self.pipeline.after_execution(report)
        self.manager.verify_now(state, "grasp")
        old_ack = self.pipeline.current()
        self.store.close()
        self.store = EventStore(self.root)
        executor = Executor(self.task, self.backend, self.store, execution_mode="process")
        manager = PlanningManager(executor, predicates())
        with self.assertRaisesRegex(ExecutionFault, "FEEDBACK_GUARD_REQUIRED"):
            manager._check_state(state)
        restored = StateCoordinator(manager, SensorTimelineAnalyzer())
        self.assertEqual(restored.current(), old_ack)
        fresh = restored.observe_current()
        self.assertEqual(manager.next(fresh).subgoal_id, "transport")
        self.assertEqual(self.store.usage()["executions"], 1)

    def test_analysis_budget_stops_after_p_updates_partial_effects(self):
        report = self.execute()
        self.pipeline.feedback.limits = FeedbackLimits(max_analyses=1)
        state = self.pipeline.after_execution(report)
        self.manager.verify_now(state, "grasp")
        second = self.execute("transport", state)
        with self.assertRaisesRegex(ExecutionFault, "COF_ANALYSIS_BUDGET_EXHAUSTED"):
            self.pipeline.after_execution(second)
        self.assertEqual(self.pipeline.current().processed_execution_id, second.execution_id)

    def test_reconciliation_is_a_new_feedback_revision_and_uses_new_state(self):
        self.backend.scenario, self.backend.stop_available = "stop_unknown", False
        report = self.execute()
        self.pipeline.after_execution(report)
        self.backend.stop_available = True
        self.backend.holding = False
        self.backend.version += 1
        reconciled = self.executor.reconcile(report.execution_id)
        self.assertEqual(reconciled.report_revision, 2)
        state = self.pipeline.after_execution(reconciled)
        self.assertFalse(next(f.value for f in state.facts if f.predicate == "holding"))
        self.assertEqual(self.pipeline.current().processed_report_revision, 2)
        self.assertEqual(self.store.usage()["executions"], 1)

    def test_p_response_after_deadline_is_not_applied(self):
        class Slow(LocalPAdapter):
            def update(self, request, previous, *, timeout_s):
                time.sleep(0.01)
                return super().update(request, previous, timeout_s=timeout_s)
        self.pipeline.adapter = Slow()
        self.pipeline.limits = StateLimits(update_timeout_s=0.001)
        request = self.pipeline._request(self.backend.observe().state)
        old = self.pipeline.current()
        with self.assertRaisesRegex(ExecutionFault, "P did not return"):
            self.pipeline.update(request)
        self.assertEqual(self.pipeline.current(), old)

    def test_unprocessed_p_update_also_blocks_without_new_execution(self):
        request = self.pipeline._request(self.backend.observe().state)
        self.store.append("PUpdateRequested", self.task.episode_id, request.model_dump(mode="json"))
        with self.assertRaisesRegex(ExecutionFault, "P_UPDATE_PENDING"):
            self.pipeline.guard_state(self.state)

    def test_removing_feedback_guard_cannot_bypass_via_executor(self):
        node = self.manager.read().plan.subgoals[0]
        code, inputs = FixedPlanner().code_for(node, 0)
        request, _ = self.manager.prepare_segment(node.id, self.state, code=code, inputs=inputs)
        self.store.feedback_guard = None
        with self.assertRaisesRegex(ExecutionFault, "FEEDBACK_GUARD_REQUIRED"):
            self.executor.execute(request)
        self.assertEqual(self.backend.calls, [])

    def test_next_model_context_contains_cof_changes_and_p_acknowledgement(self):
        state = self.pipeline.after_execution(self.execute())
        self.manager.verify_now(state, "grasp")
        client = ModelClient(ModelConfig(model="scripted", endpoint="http://127.0.0.1/unused"),
                             self.store, transport=ScriptedTransport())
        generator = ModelGenerator(self.manager, client, fixture_inputs, validate_fixture_segment,
                                   state_provider=self.pipeline.observe_current)
        context, *_ = generator._context("code")
        self.assertTrue(context["execution_feedback"]["events"])
        self.assertEqual(context["state_provenance"]["processed_execution_id"], "exec-1")
        self.assertEqual(context["execution_feedback"]["feedback_id"], context["state_provenance"]["processed_feedback_id"])


class ClosedLoopTests(unittest.TestCase):
    def scenario(self, scenario):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        result = run_closed_loop(temporary.name, scenario=scenario)
        return result, replay(temporary.name)

    def test_normal_retry_and_drop_recovery(self):
        for scenario, executions in [("normal", 3), ("grasp_retry", 4), ("drop_recovery", 5)]:
            with self.subTest(scenario=scenario):
                summary, replayed = self.scenario(scenario)
                self.assertEqual(summary["task_status"], "succeeded")
                self.assertEqual(summary["executions"], executions)
                self.assertEqual(summary["cof_reports"], executions)
                self.assertEqual(replayed["p_state"]["processed_feedback_id"], summary["processed_feedback_id"])
                self.assertIsNotNone(replayed["progress"]["final_verification_ref"])
                if scenario == "drop_recovery":
                    self.assertEqual(summary["replans"], 1)
                    self.assertTrue(any(e["predicate"] == "holding" and e["before"] and not e["after"]
                                        for f in replayed["cof_feedback"] for e in f["events"]))

    def test_occlusion_and_unknown_forever(self):
        for scenario, status, executions in [("occlusion", "succeeded", 3), ("unknown_forever", "budget_exhausted", 1)]:
            with self.subTest(scenario=scenario):
                result, _ = self.scenario(scenario)
                self.assertEqual((result["task_status"], result["executions"]), (status, executions))

    def test_missing_frames_retains_sensor_results_but_exposes_visual_gap(self):
        result, replayed = self.scenario("missing_frames")
        self.assertEqual(result["task_status"], "succeeded")
        self.assertTrue(all(f["analysis_status"] == "partial" and f["selected_frames"] == 0 for f in replayed["cof_feedback"]))

    def test_cancel_timeout_error_and_unknown_keep_stop_policy(self):
        for scenario, status in [("cancelled", "interrupted"), ("timed_out", "budget_exhausted"),
                                 ("partial_error", "blocked"), ("stop_unknown", "blocked")]:
            with self.subTest(scenario=scenario):
                result, _ = self.scenario(scenario)
                self.assertEqual(result["task_status"], status)
                self.assertEqual(result["cof_reports"], result["executions"])
                self.assertIsNotNone(result["processed_feedback_id"])

    def test_delayed_p_succeeds_and_timeout_blocks_next_action(self):
        result, _ = self.scenario("p_delayed")
        self.assertEqual(result["task_status"], "succeeded")
        result, _ = self.scenario("p_timeout")
        self.assertEqual((result["task_status"], result["executions"]), ("blocked", 1))
        self.assertEqual(result["stop_reason"], "P_ACK_TIMEOUT")

    def test_final_disturbance_is_checked_against_current_p_state(self):
        result, _ = self.scenario("final_disturbance")
        self.assertEqual(result["task_status"], "succeeded")
        self.assertEqual(result["replans"], 1)
        self.assertEqual(result["executions"], 6)

    def test_initial_goal_and_recovery_budget(self):
        result, _ = self.scenario("already_satisfied")
        self.assertEqual((result["task_status"], result["executions"]), ("succeeded", 0))
        result, _ = self.scenario("budget_exhausted")
        self.assertEqual((result["task_status"], result["executions"]), ("budget_exhausted", 3))
