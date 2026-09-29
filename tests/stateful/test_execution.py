"""Behavior tests for the foundation; no simulator, GPU, network or model required."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from capx.agents.stateful.contracts import (
    Condition, EpisodeBudget, ExecutionBudget, ExecutionRequest, StateView, TaskSpec,
)
from capx.agents.stateful.demo import evaluate_mock, lift_request, lift_task, run_demo
from capx.agents.stateful.execution.adapters.capx_backend import CapXBackend
from capx.agents.stateful.execution.adapters.fake_backend import FakeBackend
from capx.agents.stateful.execution.backend import CallContext, ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore, replay


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = lift_task("test-lift")
        self.backend = FakeBackend(self.task.episode_id)
        self.store = EventStore(self.root)
        self.executor = Executor(self.task, self.backend, self.store)
        self.request = lift_request(self.task.episode_id)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def request_with(self, **changes):
        data = self.request.model_dump(mode="json")
        data.update(changes)
        return ExecutionRequest.model_validate(data)

    def execute_scenario(self, scenario):
        self.backend.scenario = scenario
        self.backend.stop_available = scenario != "stop_unknown"
        return self.executor.execute(self.request)

    def test_normal_has_correlated_evidence_and_separate_evaluation(self):
        report = self.executor.execute(self.request)
        self.assertEqual((report.status, report.runtime_rc), ("completed", 0))
        self.assertNotIn("success", report.model_dump())
        self.assertEqual(self.backend.calls, ["close_gripper", "move_to_joints"])
        self.assertTrue(report.partial_effects_possible)
        self.assertEqual(report.state_version_before, 0)
        frames = json.loads((self.root / report.frame_manifest_ref).read_text())
        self.assertEqual([frame["boundary"] for frame in frames["frames"]],
                         ["before_execution", "after_call", "after_call", "after_execution"])
        self.assertTrue(all(frame["source"] == "mock" for frame in frames["frames"]))
        for reference in report.evidence_refs:
            self.assertTrue((self.root / reference).is_file())
        events = self.store.events()
        kinds = [e["kind"] for e in events]
        self.assertLess(kinds.index("ExecutionDispatched"), kinds.index("CallStarted"))
        self.assertEqual([e["payload"]["call_id"] for e in events if e["kind"] == "CallStarted"],
                         ["call-1", "call-2"])

    def test_normal_return_does_not_mean_grasp_success(self):
        report = self.execute_scenario("missed_grasp")
        self.assertEqual(report.status, "completed")
        state = StateView.model_validate_json((self.root / report.state_after_ref).read_text())
        self.assertEqual(evaluate_mock(self.task, state)["evaluator_outcome"], "fail")

    def test_partial_error_preserves_effects_and_stops_later_calls(self):
        request = self.request_with(code=self.request.code + "close_gripper()\n")
        self.backend.scenario = "partial_error"
        report = self.executor.execute(request)
        self.assertEqual(report.status, "error")
        self.assertTrue(self.backend.lifted)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(report.stop_reason, "API_ERROR")
        self.assertIsNotNone(report.state_after_ref)

    def test_timeout_and_cancel_require_stop_confirmation(self):
        report = self.execute_scenario("timed_out")
        self.assertEqual(report.status, "timed_out")
        self.assertEqual(report.backend_motion_state, "idle")

    def test_cancellation_signal_is_consumed(self):
        report = self.execute_scenario("cancelled")
        self.assertEqual(report.status, "cancelled")
        self.assertFalse(self.executor.cancel(self.request.execution_id))

    def test_public_cancel_signal_stops_before_next_call(self):
        invoke = self.backend.invoke

        def cancel_after_call(name, args, kwargs, context):
            result = invoke(name, args, kwargs, context)
            self.assertTrue(self.executor.cancel(context.execution_id))
            return result

        with patch.object(self.backend, "invoke", side_effect=cancel_after_call):
            report = self.executor.execute(self.request)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(self.backend.calls, ["close_gripper"])

    def test_unknown_blocks_new_ids_then_reconciles_without_replaying(self):
        report = self.execute_scenario("stop_unknown")
        self.assertEqual(report.status, "outcome_unknown")
        self.assertIsNone(report.ended_at)
        new = self.request_with(execution_id="new-execution", based_on_state_version=2)
        with self.assertRaisesRegex(ExecutionFault, "reconciliation"):
            self.executor.execute(new)
        calls = len(self.backend.calls)
        self.backend.stop_available = True
        reconciled = self.executor.reconcile(self.request.execution_id)
        self.assertEqual(reconciled.status, "error")
        self.assertEqual(reconciled.report_revision, 2)
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.executor.execute(self.request), reconciled)

    def test_duplicate_id_is_read_only_even_after_restart(self):
        report = self.executor.execute(self.request)
        usage = self.store.usage()
        self.store.close()
        self.store = EventStore(self.root)
        self.executor = Executor(self.task, self.backend, self.store)
        self.assertEqual(self.executor.execute(self.request), report)
        self.assertEqual(self.store.usage(), usage)
        self.assertEqual(len(self.backend.calls), 2)

    def test_same_id_with_different_content_is_rejected(self):
        self.executor.execute(self.request)
        with self.assertRaisesRegex(ExecutionFault, "different request"):
            self.executor.execute(self.request_with(code="close_gripper()"))
        self.assertEqual(len(self.backend.calls), 2)

    def test_lost_report_after_dispatch_never_reexecutes(self):
        self.store.register(self.request, self.task)
        self.store.close()
        self.store = EventStore(self.root)
        self.executor = Executor(self.task, self.backend, self.store)
        report = self.executor.execute(self.request)
        self.assertEqual(report.status, "outcome_unknown")
        self.assertIsNone(report.runtime_rc)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(replay(self.root)["reports"][0]["status"], "outcome_unknown")
        reconciled = self.executor.reconcile(self.request.execution_id)
        self.assertEqual(reconciled.status, "error")
        self.assertEqual(self.store.usage()["executions"], 1)

    def test_lost_report_after_side_effect_does_not_repeat_effect(self):
        with patch.object(self.store, "save_report", side_effect=OSError("disk offline")):
            with self.assertRaises(OSError):
                self.executor.execute(self.request)
        self.assertTrue(self.backend.lifted)
        self.assertEqual(self.executor.execute(self.request).status, "outcome_unknown")
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.executor.reconcile(self.request.execution_id).status, "error")

    def test_storage_failure_before_call_prevents_motion(self):
        append = self.store.append

        def fail_started(kind, eid, payload):
            if kind == "CallStarted":
                raise OSError("injected ledger failure")
            return append(kind, eid, payload)

        with patch.object(self.store, "append", side_effect=fail_started):
            report = self.executor.execute(self.request)
        self.assertEqual(report.status, "error")
        self.assertEqual(self.backend.calls, [])

    def test_dispatch_failure_prevents_motion(self):
        with patch.object(self.store, "register", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.executor.execute(self.request)
        self.assertEqual(self.backend.calls, [])

    def test_owner_lock_and_backend_session_mismatch(self):
        with self.assertRaises(ExecutionFault):
            EventStore(self.root)
        with self.assertRaisesRegex(ExecutionFault, "frozen backend"):
            Executor(self.task, FakeBackend(self.task.episode_id), self.store)

    def test_reports_are_idempotent_and_cannot_change_identity(self):
        report = self.executor.execute(self.request)
        count = len(self.store.events())
        self.store.save_report(report)
        self.assertEqual(len(self.store.events()), count)
        with self.assertRaises(ExecutionFault):
            self.store.save_report(report.model_copy(update={"episode_id": "wrong-episode"}))
        with self.assertRaises(ExecutionFault):
            self.store.save_report(report.model_copy(update={"report_revision": 2, "status": "error"}))
        stop_seq = int(report.stop_evidence_refs[0].split(":")[1])
        self.assertEqual(self.store.events()[stop_seq - 1]["kind"], "BackendStopConfirmed")

    def test_segment_and_episode_api_limits(self):
        request = self.request_with(budget=ExecutionBudget(max_api_calls=1).model_dump())
        report = self.executor.execute(request)
        self.assertEqual(report.stop_reason, "BUDGET_EXCEEDED")
        self.assertEqual(self.backend.calls, ["close_gripper"])
        self.assertEqual(self.store.usage()["api_calls"], 2)  # rejected calls are counted

    def test_episode_budget_survives_new_id_and_reopen(self):
        # Use a separately frozen run with a one-call episode budget.
        root = self.root / "budget"
        task = self.task.model_copy(update={"budget": EpisodeBudget(max_api_calls=1)})
        with EventStore(root) as store:
            executor = Executor(task, self.backend, store)
            report = executor.execute(self.request)
            self.assertEqual(report.stop_reason, "BUDGET_EXCEEDED")
        with EventStore(root) as store:
            executor = Executor(task, self.backend, store)
            request = self.request_with(execution_id="next", based_on_state_version=1,
                                        entry_conditions=[], code="close_gripper()")
            report = executor.execute(request)
            self.assertEqual(report.stop_reason, "BUDGET_EXCEEDED")
        self.assertEqual(self.backend.calls, ["close_gripper"])

    def test_episode_execution_limit(self):
        task = self.task.model_copy(update={"budget": EpisodeBudget(max_executions=1)})
        with EventStore(self.root / "segments") as store:
            executor = Executor(task, self.backend, store)
            executor.execute(self.request)
            with self.assertRaisesRegex(ExecutionFault, "execution budget"):
                executor.execute(self.request_with(execution_id="next", based_on_state_version=2,
                                                   entry_conditions=[]))

    def test_stale_state_and_unknown_negative_condition_prevent_dispatch(self):
        with self.assertRaises(ExecutionFault):
            self.executor.execute(self.request_with(based_on_state_version=10))
        unknown = Condition(id="unknown", predicate="unseen", args=["red_cube"], expected=False)
        with self.assertRaises(ExecutionFault):
            self.executor.execute(self.request_with(entry_conditions=[unknown.model_dump()]))
        self.assertEqual(self.store.usage()["executions"], 0)
        self.assertEqual(self.backend.calls, [])

    def test_old_timestamp_and_prediction_cannot_authorize_action(self):
        before = self.backend.observe()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        old_state = before.state.model_copy(update={"observed_at": stale})
        with patch.object(self.backend, "observe", return_value=type(before)(old_state, before.frames)):
            with self.assertRaises(ExecutionFault):
                self.executor.execute(self.request)
        predicted = before.state.model_copy(update={
            "facts": [fact.model_copy(update={"source": "predicted"}) for fact in before.state.facts]})
        with patch.object(self.backend, "observe", return_value=type(before)(predicted, before.frames)):
            with self.assertRaises(ExecutionFault):
                self.executor.execute(self.request)
        self.assertEqual(self.backend.calls, [])

    def test_input_and_code_validation(self):
        for code in ("", "# no code", "import os", "env.reset()", "APIS['x']()",
                     "while True: pass", "RESULT = INPUTS.__class__", "open_gripper()",
                     "close_gripper = 1", "INPUTS['x'] = 2", "RESULT = [x for x in INPUTS]"):
            with self.subTest(code=code), self.assertRaises((ExecutionFault, ValidationError)):
                self.executor.execute(self.request_with(code=code))
        self.assertEqual(self.backend.calls, [])

    def test_bad_parameters_and_nonfinite_inputs(self):
        report = self.executor.execute(self.request_with(code="move_to_joints([0.1])"))
        self.assertEqual(report.stop_reason, "ARGUMENT_INVALID")
        self.assertFalse(self.backend.lifted)
        with self.assertRaises((ValidationError, ValueError)):
            self.request_with(inputs={"lift_joints": [float("nan")]})
        with self.assertRaises(ValidationError):
            self.request_with(unrecognized=True)
        with self.assertRaises(ValidationError):
            self.request_with(inputs={"oversized": "x" * 70000})

    def test_namespace_and_result_are_reset_each_segment(self):
        first = self.executor.execute(self.request_with(code="RESULT = {'declared_success': True}"))
        second = self.executor.execute(self.request_with(execution_id="next", code="print('next')"))
        self.assertEqual(json.loads((self.root / first.result_ref).read_text()), {"declared_success": True})
        self.assertIsNone(json.loads((self.root / second.result_ref).read_text()))
        self.assertFalse(self.backend.lifted)

    def test_missing_observation_is_not_old_state_or_success(self):
        report = self.execute_scenario("observation_missing")
        self.assertEqual(report.status, "completed")
        self.assertIsNone(report.state_after_ref)
        self.assertTrue(report.feedback_missing)

    def test_frame_limit_and_stdout_truncation_are_explicit(self):
        code = "print(INPUTS['text'])\nclose_gripper()"
        report = self.executor.execute(self.request_with(
            code=code, inputs={"text": "界" * 8000},
            budget=ExecutionBudget(max_frames=1).model_dump()))
        self.assertTrue(report.output_truncated)
        self.assertLessEqual((self.root / report.stdout_ref).stat().st_size, 16384)
        manifest = json.loads((self.root / report.frame_manifest_ref).read_text())
        self.assertEqual(len(manifest["frames"]), 1)
        self.assertTrue(any("budget" in gap for gap in manifest["gaps"]))

    def test_deadline_is_checked_before_motion(self):
        with patch("capx.agents.stateful.execution.backend.time.monotonic", return_value=1e12), \
             patch("capx.agents.stateful.execution.executor.time.monotonic", side_effect=[0.0, 1e12, 1e12]):
            report = self.executor.execute(self.request)
        self.assertEqual(report.status, "timed_out")
        self.assertEqual(self.backend.calls, [])


class IntegrationTests(unittest.TestCase):
    def test_capx_environment_adapter_bypasses_legacy_exec(self):
        backend = FakeBackend("test-lift")

        class ExistingEnv:
            def api_functions(self):
                return backend.functions()

            def render(self):
                return None

            def step(self, *_args):
                raise AssertionError("must not use the legacy execution namespace")

        adapter = CapXBackend.from_env(
            ExistingEnv(), state_provider=lambda: backend.observe().state,
            motion_state=backend.motion_state, stop=backend.stop,
            validators={name: lambda *args, **kwargs: None for name in backend.functions()},
            backend_id="existing-env", catalog_version=backend.catalog_version,
        )
        with tempfile.TemporaryDirectory() as root, EventStore(root) as store:
            executor = Executor(lift_task("test-lift"), adapter, store)
            report = executor.execute(lift_request("test-lift"))
            self.assertEqual(report.status, "completed")
            self.assertTrue(backend.lifted)
            self.assertTrue(report.feedback_missing)  # render() deliberately returns no frame

    def test_capx_registry_adapter_uses_original_return_types_and_validators(self):
        backend = FakeBackend("adapter")
        adapter = CapXBackend(
            functions=backend.functions(), observe=backend.observe,
            motion_state=backend.motion_state, stop=backend.stop,
            validators={name: lambda *args, **kwargs: None for name in backend.functions()},
            backend_id="capx-test-double", catalog_version="fake_franka_lift_v1",
        )
        import threading
        import time
        context = CallContext("exec", "call", time.monotonic() + 10, threading.Event())
        self.assertIsNone(adapter.invoke("close_gripper", (), {}, context))
        self.assertTrue(backend.holding)
        self.assertFalse(adapter.profile.cooperative_deadline)
        with self.assertRaises(ValueError):
            CapXBackend(functions=backend.functions(), observe=backend.observe,
                        motion_state=backend.motion_state, stop=backend.stop, validators={},
                        backend_id="bad", catalog_version="v1")

    def test_fake_fixture_matrix_and_read_only_replay(self):
        expected = {
            "normal": ("completed", "pass"), "missed_grasp": ("completed", "fail"),
            "partial_error": ("error", "pass"), "timed_out": ("timed_out", "pass"),
            "stop_unknown": ("outcome_unknown", "unscorable"),
            "cancelled": ("cancelled", "pass"), "observation_missing": ("completed", "unscorable"),
        }
        with tempfile.TemporaryDirectory() as root:
            for scenario, pair in expected.items():
                with self.subTest(scenario=scenario):
                    folder = Path(root) / scenario
                    summary = run_demo(folder, scenario)
                    self.assertEqual((summary["execution_status"], summary["evaluator_outcome"]), pair)
                    before = (folder / "events.jsonl").read_bytes()
                    self.assertEqual(replay(folder)["reports"][0]["status"], pair[0])
                    self.assertEqual((folder / "events.jsonl").read_bytes(), before)
                    with self.assertRaises(ValueError):
                        run_demo(folder, scenario)

    def test_cli_runs_without_importing_simulators_and_exports_schemas(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, "-m", "capx.agents.stateful"]
            demo = subprocess.run(command + ["demo", "--output", root + "/run"],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(demo.stdout)[0]["execution_status"], "completed")
            playback = subprocess.run(command + ["replay", root + "/run"],
                                      capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(playback.stdout)["api_calls"], 2)
            subprocess.run(command + ["schema", "--output", root + "/schema"],
                           capture_output=True, text=True, check=True)
            self.assertTrue((Path(root) / "schema" / "ExecutionReport.json").is_file())


if __name__ == "__main__":
    unittest.main()
