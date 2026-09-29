"""Process worker, parent gateway and lifecycle behavior tests."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from capx.agents.stateful.contracts import ExecutionBudget, ExecutionRequest
from capx.agents.stateful.demo import lift_request, lift_task
from capx.agents.stateful.execution.adapters.fake_backend import FakeBackend
from capx.agents.stateful.execution.backend import ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore
from capx.agents.stateful.execution.worker import encode, run_worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EventStore(self.root)
        self.task = lift_task("worker-test")
        self.backend = FakeBackend(self.task.episode_id)
        self.executor = Executor(self.task, self.backend, self.store, execution_mode="process")
        self.request = lift_request(self.task.episode_id)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def request_with(self, **kwargs):
        return ExecutionRequest.model_validate({**self.request.model_dump(mode="json"), **kwargs})

    def test_rpc_preserves_parent_thread_affinity_and_json_result(self):
        owner = threading.get_ident()
        invoke = self.backend.invoke
        def check_thread(*args):
            self.assertEqual(threading.get_ident(), owner)
            return invoke(*args)
        with patch.object(self.backend, "invoke", side_effect=check_thread):
            report = self.executor.execute(self.request_with(code=self.request.code + "\nRESULT = {'ok': True, 'v': INPUTS['lift_joints']}"))
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.execution_mode, "process")
        self.assertTrue(json.loads((self.root / report.result_ref).read_text())["ok"])

    def test_child_environment_and_cwd_do_not_inherit_credentials(self):
        popen = subprocess.Popen
        processes = []
        def capture(*args, **kwargs):
            self.assertNotIn("WORKER_TEST_SECRET", kwargs["env"])
            self.assertEqual(set(kwargs["env"]), {"PATH", "LC_ALL"})
            self.assertNotEqual(Path(kwargs["cwd"]), self.root)
            self.assertIn("-I", args[0])
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.dict(os.environ, {"WORKER_TEST_SECRET": "secret"}), patch("subprocess.Popen", side_effect=capture):
            report = self.executor.execute(self.request)
        self.assertEqual(report.status, "completed")
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_disallowed_code_rejected_without_process_or_action(self):
        for code in ["import os", "RESULT = INPUTS.__class__", "open('/tmp/x')", "while True: pass",
                     "exec('close_gripper()')", "RESULT = b'bytes'", "RESULT = 1e999", "print('x', file=INPUTS)"]:
            with self.subTest(code=code), patch("subprocess.Popen") as spawn, self.assertRaises(ExecutionFault):
                self.executor.execute(self.request_with(code=code))
            spawn.assert_not_called()
        self.assertEqual(self.backend.calls, [])

    def test_worker_independently_rejects_import_and_attributes(self):
        for code in ["import os", "RESULT = INPUTS.__class__"]:
            result = run_worker(code, {}, [], call=lambda *_: None, checkpoint=lambda: None, timeout_s=1)
            self.assertIsNotNone(result["error"])

    def test_api_budget_is_enforced_by_parent(self):
        report = self.executor.execute(self.request_with(budget=ExecutionBudget(max_api_calls=1).model_dump(mode="json")))
        self.assertEqual(report.stop_reason, "BUDGET_EXCEEDED")
        self.assertEqual(self.backend.calls, ["close_gripper"])

    def test_cancel_prevents_following_api_calls_and_reaps_worker(self):
        invoke = self.backend.invoke
        processes = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        def cancel(*args):
            result = invoke(*args)
            self.executor.cancel(self.request.execution_id)
            return result
        with patch.object(self.backend, "invoke", side_effect=cancel), patch("subprocess.Popen", side_effect=spawn):
            report = self.executor.execute(self.request)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(self.backend.calls, ["close_gripper"])
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_deadline_can_stop_worker_without_any_api_call(self):
        report = self.executor.execute(self.request_with(code="RESULT = 1", budget={"max_wall_time_s": 0.000001}))
        self.assertEqual(report.status, "timed_out")
        self.assertEqual(self.backend.calls, [])

    def test_parent_checkpoint_can_kill_and_reap_started_worker(self):
        processes = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process
        def checkpoint():
            raise ExecutionFault("DEADLINE_EXCEEDED")
        with patch("subprocess.Popen", side_effect=spawn), self.assertRaisesRegex(ExecutionFault, "DEADLINE_EXCEEDED"):
            run_worker("RESULT = 1", {}, [], call=lambda *_: None, checkpoint=checkpoint, timeout_s=1)
        self.assertTrue(processes)
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_unknown_backend_stop_blocks_new_actions(self):
        self.backend.scenario, self.backend.stop_available = "stop_unknown", False
        report = self.executor.execute(self.request)
        self.assertEqual(report.status, "outcome_unknown")
        with self.assertRaises(ExecutionFault):
            self.executor.execute(self.request_with(execution_id="next", based_on_state_version=self.backend.version))
        self.assertEqual(self.backend.calls, ["close_gripper", "move_to_joints"])

    def test_backend_object_cannot_cross_rpc(self):
        with patch.object(self.backend, "invoke", return_value=object()):
            report = self.executor.execute(self.request)
        self.assertEqual(report.stop_reason, "API_RESULT_UNSUPPORTED")
        self.assertEqual(report.api_calls, 1)

    def test_large_backend_result_stops_later_calls(self):
        with patch.object(self.backend, "invoke", return_value="x" * 150000):
            report = self.executor.execute(self.request)
        self.assertEqual(report.stop_reason, "API_RESULT_UNSUPPORTED")
        self.assertEqual(report.api_calls, 1)

    def test_stdout_is_bounded_and_truncation_is_explicit(self):
        report = self.executor.execute(self.request_with(code="print(INPUTS['text'])\nRESULT = 1", inputs={"text": "x" * 20000}))
        self.assertEqual(report.status, "completed")
        stdout = (self.root / f"executions/{self.request.execution_id}/stdout.txt").read_bytes()
        self.assertLessEqual(len(stdout), 16384)
        self.assertTrue(report.output_truncated)

    def test_large_result_and_alias_expansion_are_bounded(self):
        # A compact script can form an exponentially large JSON expansion via shared lists.
        code = "a = [0]\n" + "a = [a, a]\n" * 25 + "RESULT = a\nclose_gripper()"
        report = self.executor.execute(self.request_with(code=code))
        self.assertEqual(report.status, "error")
        self.assertEqual(self.backend.calls, [])
        value = [0]
        for _ in range(25):
            value = [value, value]
        with self.assertRaisesRegex(ValueError, "message exceeds limit"):
            encode(value)

    def test_each_segment_has_a_fresh_namespace_and_duplicate_id_is_read_only(self):
        first = self.executor.execute(self.request_with(code="x = 123\nRESULT = x"))
        self.assertEqual(self.executor.execute(self.request_with(code="x = 123\nRESULT = x")), first)
        # x cannot be loaded in a new program, even though the old process defined it.
        with self.assertRaises(ExecutionFault):
            self.executor.execute(self.request_with(execution_id="second", code="RESULT = x"))
        self.assertEqual(self.store.usage()["executions"], 1)
