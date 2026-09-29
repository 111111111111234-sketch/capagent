"""Planning behavior tests using the first-stage executor and the same ledger."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from capx.agents.stateful.contracts import Condition, TaskSpec
from capx.agents.stateful.execution.backend import ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore, replay
from capx.agents.stateful.planning.contracts import (
    PlanPatch, PlanProposal, PlanningLimits, Subgoal, VerificationReport,
)
from capx.agents.stateful.planning.demo import run_planning_demo
from capx.agents.stateful.planning.fixtures import (
    HOLDING, LIFTED, OPEN, FixedPlanner, StackingBackend, predicates, stack_task,
)
from capx.agents.stateful.planning.manager import PlanningManager
from capx.agents.stateful.planning.verification import verify


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = stack_task("test-plan")
        self.backend = StackingBackend(self.task.episode_id)
        self.store = EventStore(self.root)
        self.executor = Executor(self.task, self.backend, self.store)
        self.planner = FixedPlanner()
        self.manager = PlanningManager(self.executor, predicates())
        self.proposal = self.planner.create_plan(self.state())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def state(self):
        return self.backend.observe().state

    def create(self):
        return self.manager.create_plan(self.proposal, self.state())

    def execute(self, node_id):
        current = self.manager.read()
        node = next(node for node in current.plan.subgoals if node.id == node_id)
        code, inputs = self.planner.code_for(node, current.progress.subgoals[node.id].attempt_count)
        return self.manager.execute_segment(node_id, self.state(), code=code, inputs=inputs)

    def finish_node(self, node_id):
        report = self.execute(node_id)
        self.manager.verify_now(self.state(), node_id)
        return report

    def test_invalid_plans_are_rejected_before_any_dispatch(self):
        base = self.proposal.model_dump()
        variants = []
        for mutation in (
            lambda p: p["subgoals"].append(p["subgoals"][0].copy()),
            lambda p: p["subgoals"][0].update(depends_on=["place"]),
            lambda p: p["subgoals"][0].update(depends_on=["absent"]),
            lambda p: p["subgoals"][0].update(candidate_skills=["teleport"]),
            lambda p: p["subgoals"][0]["preconditions"][0].update(predicate="invented"),
            lambda p: p["subgoals"][0]["preconditions"][0].update(args=["absent-object"]),
            lambda p: p.update(goal_coverage={}),
            lambda p: p["goal_coverage"].update(on_green=["grasp"]),
            lambda p: p["subgoals"][2]["success_conditions"][0].update(expected=False),
        ):
            candidate = json.loads(json.dumps(base))
            mutation(candidate)
            variants.append(candidate)
        for candidate in variants:
            with self.subTest(candidate=candidate), self.assertRaises((ExecutionFault, ValidationError)):
                self.manager.create_plan(PlanProposal.model_validate(candidate), self.state())
        self.assertEqual(self.store.usage()["executions"], 0)
        self.assertEqual(self.backend.calls, [])

    def test_plan_cannot_write_progress_or_weaken_task(self):
        data = self.proposal.model_dump()
        data["subgoals"][0]["status"] = "succeeded"
        with self.assertRaises(ValidationError):
            PlanProposal.model_validate(data)
        with self.assertRaises(ValidationError):
            PlanPatch(base_plan_version=1, base_progress_revision=0, based_on_state_version=0,
                      reason="easier", task_goal_conditions=[])

    def test_dependency_order_and_verification_gate(self):
        self.create()
        self.assertEqual(self.manager.next(self.state()).subgoal_id, "grasp")
        with self.assertRaises(ExecutionFault):
            self.execute("transport")
        self.execute("grasp")
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "awaiting_verification")
        self.assertEqual(self.manager.next(self.state()).kind, "verify_subgoal")
        with self.assertRaises(ExecutionFault):
            self.execute("transport")
        self.manager.verify_now(self.state(), "grasp")
        self.assertEqual(self.manager.next(self.state()).subgoal_id, "transport")

    def test_runtime_zero_with_empty_grasp_needs_recovery(self):
        self.backend.planning_scenario = "grasp_retry"
        self.create()
        report = self.execute("grasp")
        self.assertEqual(report.runtime_rc, 0)
        self.manager.verify_now(self.state(), "grasp")
        progress = self.manager.read().progress
        self.assertEqual(progress.subgoals["grasp"].status, "needs_recovery")
        self.assertEqual(progress.task_status, "active")
        self.finish_node("grasp")
        current = self.manager.read()
        self.assertEqual(current.progress.recovery_attempts["grasp"], 2)
        self.assertEqual(current.plan.plan_version, 1)
        scripts = [req.code for req in current.requests.values()]
        self.assertNotEqual(scripts[0], scripts[1])

    def test_unknown_requests_observation_without_retrying(self):
        self.create()
        self.execute("grasp")
        self.backend.occluded = True
        self.manager.verify_now(self.state(), "grasp")
        self.assertEqual(self.manager.next(self.state()).kind, "request_observation")
        before = len(self.backend.calls)
        with self.assertRaises(ExecutionFault):
            self.execute("grasp")
        self.backend.occluded = False
        self.manager.observe()
        self.assertEqual(self.manager.next(self.state()).kind, "verify_subgoal")
        self.manager.verify_now(self.state(), "grasp")
        self.assertEqual(len(self.backend.calls), before)
        self.assertEqual(self.manager.read().progress.recovery_attempts["grasp"], 1)

    def test_unknown_negative_precondition_is_not_satisfied(self):
        negative = Condition(id="empty", predicate="holding", args=["robot", "red_cube"], expected=False)
        nodes = list(self.proposal.subgoals)
        nodes[0] = nodes[0].model_copy(update={"preconditions": [negative]})
        self.proposal = self.proposal.model_copy(update={"subgoals": nodes})
        self.create()
        self.backend.occluded = True
        self.assertEqual(self.manager.next(self.state()).kind, "request_observation")
        self.assertEqual(self.backend.calls, [])

    def test_duplicate_verification_is_idempotent_and_conflict_rejected(self):
        self.create()
        self.execute("grasp")
        request = self.manager.request_verification("grasp")
        state = self.state()
        report = verify(request, state, max_age=30., source="mock")
        before = self.manager.apply_verification(report, state)
        self.assertEqual(self.manager.apply_verification(report, state), before)
        with self.assertRaises(ExecutionFault):
            self.manager.apply_verification(report.model_copy(update={"subject_id": "transport"}), state)
        second = report.model_copy(update={"report_id": "another-id"})
        with self.assertRaises(ExecutionFault):
            self.manager.apply_verification(second, state)

    def test_forged_and_wrong_episode_verification_cannot_advance(self):
        self.backend.planning_scenario = "budget_exhausted"
        self.create()
        self.execute("grasp")
        request = self.manager.request_verification("grasp")
        state = self.state()
        report = verify(request, state, max_age=30., source="mock")
        data = report.model_dump()
        for check in data["groups"]["subgoal"]:
            check["verdict"] = "pass"
        with self.assertRaises(ExecutionFault):
            self.manager.apply_verification(VerificationReport.model_validate(data), state)
        with self.assertRaises(ExecutionFault):
            self.manager.apply_verification(report.model_copy(update={"episode_id": "different"}), state)
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "awaiting_verification")

    def test_execution_pre_state_and_predicted_facts_cannot_prove_completion(self):
        self.create()
        before = self.state()
        self.execute("grasp")
        request = self.manager.request_verification("grasp")
        with self.assertRaises(ExecutionFault):
            verify(request, before, max_age=30., source="mock")
        state = self.state()
        state = state.model_copy(update={"facts": [fact.model_copy(update={"source": "predicted"}) for fact in state.facts]})
        self.manager.apply_verification(verify(request, state, max_age=30., source="mock"), state)
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "awaiting_verification")

    def test_late_check_after_another_dispatch_is_rejected(self):
        self.create()
        self.finish_node("grasp")
        request = self.manager.request_verification()
        state = self.state()
        report = verify(request, state, max_age=30., source="mock")
        self.execute("transport")
        with self.assertRaisesRegex(ExecutionFault, "VERIFICATION_STALE"):
            self.manager.apply_verification(report, state)

    def test_current_preconditions_override_historical_success(self):
        self.create()
        self.finish_node("grasp")
        self.backend.holding = self.backend.lifted = False
        self.backend.version += 1
        self.assertEqual(self.manager.next(self.state()).kind, "revise_plan")
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "succeeded")
        with self.assertRaises(ExecutionFault):
            self.execute("transport")

    def test_local_patch_keeps_history_and_rewires_dependents(self):
        self.backend.planning_scenario = "drop_recovery"
        self.create()
        self.finish_node("grasp")
        self.finish_node("transport")
        old = self.manager.read()
        patch_value = self.planner.revise_plan(old, self.state())
        self.manager.commit_patch(patch_value, self.state())
        current = self.manager.read()
        self.assertEqual(current.plan.plan_version, 2)
        self.assertEqual(current.progress.subgoals["grasp"], old.progress.subgoals["grasp"])
        self.assertEqual(current.progress.subgoals["transport"].status, "superseded")
        place = next(node for node in current.plan.subgoals if node.id == "place")
        self.assertEqual(place.depends_on, ["recover-transport-2"])
        self.assertEqual(current.progress.recovery_attempts, old.progress.recovery_attempts)

    def prepare_patch(self):
        self.backend.planning_scenario = "drop_recovery"
        self.create()
        self.finish_node("grasp")
        self.finish_node("transport")
        return self.planner.revise_plan(self.manager.read(), self.state())

    def test_patch_cannot_reset_budget_or_delete_successful_history(self):
        patch_value = self.prepare_patch()
        bad_node = patch_value.add_subgoals[0].model_copy(update={"recovery_group_id": "fresh-budget"})
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"add_subgoals": [bad_node]}), self.state())
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"retire_subgoals": ["grasp"]}), self.state())
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"dependency_updates": {}}), self.state())
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"goal_coverage": {}}), self.state())

    def test_recovery_cannot_borrow_another_purposes_budget(self):
        patch_value = self.prepare_patch()
        node = patch_value.add_subgoals[0].model_copy(update={"recovery_of": "place", "recovery_group_id": "place"})
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"add_subgoals": [node]}), self.state())

    def test_equivalent_initial_goals_share_budget_even_with_renamed_conditions(self):
        duplicate = self.proposal.subgoals[0].model_copy(update={
            "id": "grasp-copy", "recovery_group_id": "fresh-budget",
            "success_conditions": [c.model_copy(update={"id": "renamed-" + c.id}) for c in self.proposal.subgoals[0].success_conditions]})
        self.proposal = self.proposal.model_copy(update={"subgoals": self.proposal.subgoals + [duplicate]})
        with self.assertRaises(ExecutionFault):
            self.create()

    def test_patch_requires_current_versions_and_no_pending_verification(self):
        patch_value = self.prepare_patch()
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(patch_value.model_copy(update={"base_progress_revision": 0}), self.state())
        self.manager.commit_patch(patch_value, self.state())
        self.execute("recover-grasp-2")
        new = self.planner.revise_plan(self.manager.read(), self.state())
        with self.assertRaises(ExecutionFault):
            self.manager.commit_patch(new, self.state())

    def test_normal_release_does_not_trigger_regrasp(self):
        self.create()
        for node in ("grasp", "transport", "place"):
            self.finish_node(node)
        self.assertFalse(self.backend.holding)
        self.assertEqual(self.manager.next(self.state()).kind, "verify_final")
        self.manager.verify_now(self.state())
        self.assertEqual(self.manager.read().progress.task_status, "succeeded")
        self.assertEqual(self.store.usage()["executions"], 3)

    def test_final_goal_must_hold_now_not_only_in_history(self):
        self.create()
        for node in ("grasp", "transport", "place"):
            self.finish_node(node)
        self.backend.on = False
        self.backend.version += 1
        self.assertEqual(self.manager.next(self.state()).kind, "verify_final")
        self.manager.verify_now(self.state())
        self.assertEqual(self.manager.read().progress.task_status, "active")
        self.assertEqual(self.manager.next(self.state()).kind, "revise_plan")

    def test_initially_satisfied_task_needs_verification_but_no_action(self):
        self.backend.on = True
        self.create()
        self.assertEqual(self.manager.next(self.state()).kind, "verify_final")
        self.assertEqual(self.manager.read().progress.task_status, "active")
        self.manager.verify_now(self.state())
        self.assertEqual(self.manager.read().progress.task_status, "succeeded")
        self.assertEqual(self.backend.calls, [])

    def test_satisfied_subgoal_is_skipped_with_evidence(self):
        self.backend.holding = self.backend.lifted = True
        self.create()
        self.assertEqual(self.manager.next(self.state()).kind, "verify_subgoal")
        self.manager.verify_now(self.state(), "grasp")
        progress = self.manager.read().progress
        self.assertEqual(progress.subgoals["grasp"].status, "skipped_verified")
        self.assertEqual(progress.subgoals["grasp"].attempt_count, 0)

    def test_runtime_error_retained_even_if_goal_effect_is_verified(self):
        self.create()
        self.backend.scenario = "partial_error"
        self.assertEqual(self.execute("grasp").status, "error")
        self.manager.verify_now(self.state(), "grasp")
        progress = self.manager.read().progress
        self.assertEqual(progress.subgoals["grasp"].status, "succeeded")
        self.assertEqual(progress.task_status, "blocked")

    def test_cancel_and_resume_preserve_progress_and_budget(self):
        self.create()
        self.backend.scenario = "cancelled"
        self.execute("grasp")
        self.manager.verify_now(self.state(), "grasp")
        usage = self.store.usage()
        self.assertEqual(self.manager.read().progress.task_status, "interrupted")
        self.backend.scenario = "normal"
        self.manager.resume(self.state())
        self.assertEqual(self.manager.next(self.state()).subgoal_id, "transport")
        self.assertEqual(self.store.usage(), usage)

    def test_cancel_policy_survives_crash_after_verification_event(self):
        self.create()
        self.backend.scenario = "cancelled"
        self.execute("grasp")
        with patch.object(self.manager, "export", side_effect=OSError("snapshot write failed")):
            with self.assertRaises(OSError):
                self.manager.verify_now(self.state(), "grasp")
        restored = replay(self.root)["progress"]
        self.assertEqual(restored["subgoals"]["grasp"]["status"], "succeeded")
        self.assertEqual(restored["task_status"], "interrupted")

    def test_restart_rebuilds_from_events_and_does_not_repeat_motion(self):
        self.create()
        with patch.object(self.store, "save_report", side_effect=OSError("crash before final report")):
            with self.assertRaises(OSError):
                self.execute("grasp")
        usage = self.store.usage()
        self.store.close()
        (self.root / "planning" / "progress.json").write_text("invalid snapshot")
        self.store = EventStore(self.root)
        self.executor = Executor(self.task, self.backend, self.store)
        self.manager = PlanningManager(self.executor, predicates())
        self.assertEqual(self.manager.next(self.state()).kind, "wait_for_execution")
        self.assertEqual(self.manager.read().progress.recovery_attempts, {"grasp": 1})
        self.assertEqual(self.store.usage(), usage)
        self.executor.reconcile("exec-1")
        self.assertEqual(self.manager.next(self.state()).kind, "verify_subgoal")
        self.manager.verify_now(self.state(), "grasp")
        self.assertEqual(self.store.usage(), usage)

    def test_bypassing_planning_manager_after_restart_is_rejected(self):
        self.create()
        self.finish_node("grasp")
        request = self.manager.read().requests["exec-1"].model_copy(update={
            "execution_id": "bypass", "based_on_state_version": self.backend.version})
        self.store.close()
        self.store = EventStore(self.root)
        executor = Executor(self.task, self.backend, self.store)
        with self.assertRaisesRegex(ExecutionFault, "restore the planning manager"):
            executor.execute(request)
        self.assertEqual(self.store.usage()["executions"], 1)

    def test_schema_errors_do_not_consume_attempts(self):
        self.create()
        with self.assertRaises(ExecutionFault):
            self.manager.execute_segment("grasp", self.state(), code="import os")
        self.assertEqual(self.manager.read().progress.recovery_attempts, {})
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].attempt_count, 0)

    def test_failed_dispatch_transaction_does_not_consume_attempt(self):
        self.create()
        record = self.store._event

        def fail_dispatch(kind, eid, payload):
            if kind == "ExecutionDispatched":
                raise OSError("dispatch transaction failed")
            return record(kind, eid, payload)

        with patch.object(self.store, "_event", side_effect=fail_dispatch):
            with self.assertRaises(OSError):
                self.execute("grasp")
        self.assertEqual(self.store.usage()["executions"], 0)
        self.assertEqual(self.manager.read().progress.recovery_attempts, {})
        self.assertEqual(self.backend.calls, [])

    def test_planning_limits_are_frozen_across_manager_reconstruction(self):
        self.create()
        with self.assertRaises(ExecutionFault):
            PlanningManager(self.executor, predicates(), PlanningLimits(max_attempts_per_recovery_group=99))

    def test_final_verification_cannot_skip_unchecked_execution(self):
        self.create()
        self.execute("grasp")
        with self.assertRaises(ExecutionFault):
            self.manager.request_verification()

    def test_success_cannot_be_set_by_termination_or_script_text(self):
        self.create()
        with self.assertRaises(ExecutionFault):
            self.manager.terminate("succeeded", "I did it")
        self.manager.execute_segment("grasp", self.state(), code="RESULT = {'success': True}")
        self.manager.verify_now(self.state(), "grasp")
        self.assertEqual(self.manager.read().progress.task_status, "active")


class PlanningIntegrationTests(unittest.TestCase):
    def test_replanning_limit_stops_without_resetting_runtime_budget(self):
        with tempfile.TemporaryDirectory() as root, EventStore(root) as store:
            task, planner = stack_task("limited"), FixedPlanner()
            backend = StackingBackend(task.episode_id, "drop_recovery")
            manager = PlanningManager(Executor(task, backend, store), predicates(), PlanningLimits(max_replans=1))
            manager.create_plan(planner.create_plan(backend.observe().state), backend.observe().state)
            for node_id in ("grasp", "transport"):
                node = next(node for node in manager.read().plan.subgoals if node.id == node_id)
                code, inputs = planner.code_for(node, 0)
                manager.execute_segment(node_id, backend.observe().state, code=code, inputs=inputs)
                manager.verify_now(backend.observe().state, node_id)
            usage = store.usage()
            manager.commit_patch(planner.revise_plan(manager.read(), backend.observe().state), backend.observe().state)
            with self.assertRaises(ExecutionFault):
                manager.commit_patch(planner.revise_plan(manager.read(), backend.observe().state), backend.observe().state)
            self.assertEqual(manager.read().progress.task_status, "budget_exhausted")
            self.assertEqual(manager.read().progress.replans, 1)
            self.assertEqual(store.usage(), usage)

    def test_multiple_segments_use_one_attempt_and_recheck_segment_entry(self):
        with tempfile.TemporaryDirectory() as root, EventStore(root) as store:
            task = TaskSpec(episode_id="multi", task_id="lift", instruction="hold and lift", goal_conditions=[HOLDING, LIFTED])
            backend = StackingBackend("multi")
            manager = PlanningManager(Executor(task, backend, store), predicates())
            state = backend.observe().state
            proposal = PlanProposal(based_on_state_version=0, goal_coverage={HOLDING.id: ["lift"], LIFTED.id: ["lift"]}, subgoals=[
                Subgoal(id="lift", goal="grasp then lift", preconditions=[OPEN], success_conditions=[HOLDING, LIFTED],
                        candidate_skills=["close_gripper", "move_to_joints"], recovery_group_id="lift")])
            manager.create_plan(proposal, state)
            manager.execute_segment("lift", state, code="close_gripper()", expected_conditions=[HOLDING], continue_conditions=[HOLDING])
            manager.verify_now(backend.observe().state, "lift")
            self.assertEqual(manager.next(backend.observe().state).kind, "continue_subgoal")
            manager.execute_segment("lift", backend.observe().state, code="move_to_joints(INPUTS['joints'])",
                                    inputs={"joints": [0.2, 0., 0., 0., 0., 0., 0.]}, entry_conditions=[HOLDING])
            manager.verify_now(backend.observe().state, "lift")
            progress = manager.read().progress
            self.assertEqual(progress.subgoals["lift"].status, "succeeded")
            self.assertEqual(progress.subgoals["lift"].attempt_count, 1)
            self.assertEqual(progress.attempt_segments, {"attempt-1": 2})
            self.assertFalse(backend.gripper_open)

    def test_fixture_matrix_and_read_only_replay(self):
        expected = {"normal": ("succeeded", 1, 3), "grasp_retry": ("succeeded", 1, 4),
                    "drop_recovery": ("succeeded", 2, 5), "occlusion": ("succeeded", 1, 3),
                    "unknown_forever": ("budget_exhausted", 1, 1), "budget_exhausted": ("budget_exhausted", 1, 3),
                    "already_satisfied": ("succeeded", 1, 0), "final_disturbance": ("succeeded", 2, 6),
                    "stop_unknown": ("interrupted", 1, 2)}
        with tempfile.TemporaryDirectory() as root:
            for scenario, result in expected.items():
                with self.subTest(scenario=scenario):
                    directory = Path(root) / scenario
                    summary = run_planning_demo(directory, scenario)
                    self.assertEqual((summary["task_status"], summary["plan_version"], summary["executions"]), result)
                    replayed = replay(directory)
                    self.assertEqual(replayed["progress"]["task_status"], result[0])
                    self.assertEqual(replayed["plan"]["plan_version"], result[1])
                    if scenario == "unknown_forever":
                        self.assertEqual(replayed["progress"]["observations_without_progress"], 3)
                    if scenario == "budget_exhausted":
                        self.assertEqual(replayed["progress"]["recovery_attempts"]["grasp"], 3)
                    if scenario == "drop_recovery":
                        self.assertEqual(replayed["progress"]["subgoals"]["grasp"]["status"], "succeeded")
                        self.assertEqual(replayed["progress"]["subgoals"]["transport"]["status"], "superseded")

    def test_cli_planning_and_schema_export(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-m", "capx.agents.stateful"]
            run = subprocess.run(command + ["plan-demo", "--scenario", "drop_recovery", "--output", root + "/demo"],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(run.stdout)[0]["plan_version"], 2)
            subprocess.run(command + ["schema", "--output", root + "/schemas"], capture_output=True, check=True)
            self.assertTrue((Path(root) / "schemas" / "TaskProgress.json").is_file())


if __name__ == "__main__":
    unittest.main()
