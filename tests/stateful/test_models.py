"""Offline protocol and repair tests. No request reaches a real model service."""

import io
import json
import os
import base64
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from capx.agents.stateful.contracts import canonical_json
from capx.agents.stateful.execution.backend import ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore, replay
from capx.agents.stateful.models.client import HTTPReply, HTTPTransport, ModelClient, ModelFault, NoRedirect, strict_json
from capx.agents.stateful.models.contracts import ModelConfig, ModelLimits
from capx.agents.stateful.models.fixtures import ScriptedTransport, run_model_test_demo
from capx.agents.stateful.models.generator import ModelGenerator
from capx.agents.stateful.models.runner import fixture_inputs, run_model_loop, validate_fixture_segment
from capx.agents.stateful.planning.fixtures import FixedPlanner, StackingBackend, predicates, stack_task
from capx.agents.stateful.planning.manager import PlanningManager


def reply(content="{}", *, finish="stop", usage=None, message_extra=None):
    data = {"choices": [{"finish_reason": finish, "message": {"content": content, **(message_extra or {})}}]}
    if usage is not None:
        data["usage"] = usage
    return HTTPReply(200, canonical_json(data).encode())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.temp.name)
        self.config = ModelConfig(model="test-model", endpoint="https://models.example/v1/chat/completions")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def client(self, transport, config=None, limits=None):
        return ModelClient(config or self.config, self.store, limits, transport=transport)

    def test_request_protocol_and_usage(self):
        transport = Mock(return_value=reply('{"ok":true}', usage={"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}))
        config = self.config.model_copy(update={"token_parameter": "max_completion_tokens", "json_object_mode": True})
        client = self.client(transport, config)
        self.assertEqual(client.complete([{"role": "user", "content": "test"}], purpose="plan"), '{"ok":true}')
        endpoint, headers, body, timeout, limit = transport.call_args.args
        data = strict_json(body.decode())
        self.assertEqual(endpoint, config.endpoint)
        self.assertEqual(data["max_completion_tokens"], 2048)
        self.assertEqual(data["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", data)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(client.usage()["reported_completion_tokens"], 8)
        self.assertEqual(client.usage()["calls_without_completion_usage"], 0)

    def test_key_selected_from_named_environment_and_not_logged(self):
        key = "secret-for-test-do-not-log"
        transport = Mock(return_value=reply(key))
        client = self.client(transport, self.config.model_copy(update={"api_key_env": "CAPX_TEST_KEY"}))
        with patch.dict(os.environ, {"CAPX_TEST_KEY": key}):
            self.assertEqual(client.complete([], purpose="plan"), "[REDACTED]")
        self.assertEqual(transport.call_args.args[1]["Authorization"], f"Bearer {key}")
        self.assertNotIn(key, canonical_json(self.store.events()))
        for path in Path(self.temp.name).rglob("*.json"):
            self.assertNotIn(key, path.read_text())

    def test_missing_key_fails_before_call(self):
        transport = Mock()
        client = self.client(transport, self.config.model_copy(update={"api_key_env": "CAPX_ABSENT_TEST_KEY"}))
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ModelFault, "KEY_ENV_MISSING"):
            client.complete([], purpose="plan")
        transport.assert_not_called()
        self.assertEqual(client.usage()["model_calls"], 0)

    def test_transient_retry_counts_each_http_attempt(self):
        transport = Mock(side_effect=[HTTPReply(503, b"sensitive error body"), reply()])
        client = self.client(transport)
        client.complete([], purpose="plan")
        self.assertEqual(client.usage()["model_calls"], 2)
        self.assertEqual(client.usage()["reserved_output_tokens"], 4096)
        self.assertNotIn("sensitive error body", canonical_json(self.store.events()))

    def test_retries_stop_at_configured_limit(self):
        transport = Mock(return_value=HTTPReply(503, b""))
        with self.assertRaisesRegex(ModelFault, "HTTP_503"):
            self.client(transport).complete([], purpose="plan")
        self.assertEqual(transport.call_count, 2)

    def test_auth_error_does_not_retry(self):
        transport = Mock(return_value=HTTPReply(401, b""))
        with self.assertRaisesRegex(ModelFault, "HTTP_401"):
            self.client(transport).complete([], purpose="plan")
        self.assertEqual(transport.call_count, 1)

    def test_budget_prevents_retry(self):
        transport = Mock(return_value=HTTPReply(429, b""))
        client = self.client(transport, limits=ModelLimits(max_calls=1))
        with self.assertRaisesRegex(ModelFault, "BUDGET_EXHAUSTED"):
            client.complete([], purpose="plan")
        self.assertEqual(transport.call_count, 1)

    def test_token_reservation_prevents_overspending(self):
        transport = Mock(return_value=reply())
        client = self.client(transport, limits=ModelLimits(max_reserved_output_tokens=2048))
        client.complete([], purpose="plan")
        with self.assertRaisesRegex(ModelFault, "BUDGET_EXHAUSTED"):
            client.complete([], purpose="code")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(client.usage()["calls_without_completion_usage"], 1)

    def test_restart_preserves_spent_and_unknown_reservations(self):
        transport = Mock(return_value=reply())
        limits = ModelLimits(max_calls=1)
        client = self.client(transport, limits=limits)
        client.complete([], purpose="plan")
        self.store.close()
        self.store = EventStore(self.temp.name)
        client = self.client(transport, limits=limits)
        with self.assertRaisesRegex(ModelFault, "BUDGET_EXHAUSTED"):
            client.complete([], purpose="plan")
        self.assertEqual(transport.call_count, 1)

    def test_large_context_rejected_before_network(self):
        transport = Mock()
        client = self.client(transport, limits=ModelLimits(max_context_bytes=4096))
        with self.assertRaisesRegex(ModelFault, "CONTEXT_TOO_LARGE"):
            client.complete([{"role": "user", "content": "x" * 5000}], purpose="code")
        transport.assert_not_called()

    def test_large_response_rejected(self):
        client = self.client(Mock(return_value=HTTPReply(200, b"x" * 5000)), limits=ModelLimits(max_response_bytes=4096))
        with self.assertRaisesRegex(ModelFault, "RESPONSE_TOO_LARGE"):
            client.complete([], purpose="plan")

    def test_truncated_response_counts_usage_but_cannot_be_a_proposal(self):
        client = self.client(Mock(return_value=reply(finish="length", usage={"completion_tokens": 2048})))
        with self.assertRaisesRegex(ModelFault, "RESPONSE_INCOMPLETE"):
            client.complete([], purpose="plan")
        self.assertEqual(client.usage()["reported_completion_tokens"], 2048)

    def test_empty_refusal_and_tool_output_are_rejected(self):
        client = self.client(Mock())
        for response in [reply(""), reply(message_extra={"refusal": "no"}),
                         reply(message_extra={"tool_calls": [{"id": "call"}]})]:
            with self.subTest(response=response):
                client.transport.return_value = response
                with self.assertRaises(ModelFault):
                    client.complete([], purpose="plan")

    def test_invalid_envelopes_fail_cleanly(self):
        client = self.client(Mock())
        for body in [b"not JSON", b"[]", b'{"choices":[]}', b'{"choices":[null]}',
                     b'{"choices":[],"choices":[]}']:
            client.transport.return_value = HTTPReply(200, body)
            with self.subTest(body=body), self.assertRaises(ModelFault):
                client.complete([], purpose="plan")

    def test_strict_json_rejects_duplicate_nonfinite_and_fenced_output(self):
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '```json\n{}\n```']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json(raw)

    def test_configuration_requires_explicit_safe_endpoint_and_no_raw_key(self):
        for endpoint in ["", "https://key:secret@example.com/v1", "https://example.com/v1?key=secret",
                         "http://remote.example/v1", "file:///tmp/model", "https://example.com/v1#secret"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                ModelConfig(model="test", endpoint=endpoint)
        with self.assertRaises(ValidationError):
            ModelConfig(model="test", endpoint=self.config.endpoint, api_key="secret")

    def test_http_transport_uses_post_bounds_and_timeout_without_network(self):
        stream = io.BytesIO(b"{}")
        stream.status = 200
        opener = Mock()
        opener.open.return_value = stream
        response = HTTPTransport(opener)(self.config.endpoint, {"Accept": "application/json"}, b"{}", 2.0, 4096)
        self.assertEqual(response.body, b"{}")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 2.0)

    def test_http_redirect_and_errors_never_forward_or_log_error_body(self):
        request = urllib.request.Request(self.config.endpoint, headers={"Authorization": "Bearer secret"})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example"))
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(self.config.endpoint, 302, "Found", {}, io.BytesIO(b"secret"))
        response = HTTPTransport(opener)(self.config.endpoint, {}, b"{}", 1.0, 4096)
        self.assertEqual(response, HTTPReply(302, b""))

    def test_http_timeout_is_retryable(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("sensitive connection string")
        with self.assertRaises(ModelFault) as raised:
            HTTPTransport(opener)(self.config.endpoint, {}, b"{}", 1.0, 4096)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("sensitive", str(raised.exception))

    def test_default_transport_keeps_key_off_command_line_and_uses_worker(self):
        process = Mock()
        process.returncode = 0
        process.poll.return_value = 0
        process.communicate.return_value = (canonical_json({"status": 200, "body": base64.b64encode(b"{}").decode()}).encode(), None)
        with patch("subprocess.Popen", return_value=process) as spawn:
            result = HTTPTransport()(self.config.endpoint, {"Authorization": "Bearer secret"}, b"{}", 2.0, 4096)
        self.assertEqual(result, HTTPReply(200, b"{}"))
        self.assertNotIn("secret", str(spawn.call_args))
        self.assertIn("http_worker.py", spawn.call_args.args[0][-1])
        self.assertIn("Bearer secret", process.communicate.call_args_list[0].args[0].decode())
        self.assertEqual(process.communicate.call_args_list[0].kwargs["timeout"], 2.0)

    def test_total_http_deadline_kills_and_reaps_hung_worker_without_network(self):
        processes = []
        popen = subprocess.Popen
        def hung(*args, **kwargs):
            process = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            return process
        with patch("subprocess.Popen", side_effect=hung), self.assertRaisesRegex(ModelFault, "MODEL_TIMEOUT"):
            HTTPTransport()(self.config.endpoint, {}, b"{}", 0.05, 4096)
        self.assertTrue(processes)
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_http_response_body_limit_is_enforced_during_read(self):
        stream = io.BytesIO(b"x" * 5000)
        stream.status = 200
        opener = Mock()
        opener.open.return_value = stream
        with self.assertRaisesRegex(ModelFault, "RESPONSE_TOO_LARGE"):
            HTTPTransport(opener)(self.config.endpoint, {}, b"{}", 2.0, 4096)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.temp.name)
        self.task = stack_task("generation-test")
        self.backend = StackingBackend(self.task.episode_id)
        self.manager = PlanningManager(Executor(self.task, self.backend, self.store, execution_mode="process"), predicates())
        self.scripted = ScriptedTransport()
        self.contexts = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def generator(self, mutation=None, *, limits=None):
        def transport(*args):
            context = strict_json(strict_json(args[2].decode())["messages"][-1]["content"])
            self.contexts.append(context)
            response = self.scripted(*args)
            if mutation:
                data = strict_json(response.body.decode())
                content = strict_json(data["choices"][0]["message"]["content"])
                changed = mutation(context, content)
                response = reply(changed if isinstance(changed, str) else canonical_json(changed))
            return response
        client = ModelClient(ModelConfig(model="scripted", endpoint="http://127.0.0.1/unused"),
                             self.store, limits, transport=transport, setting="scripted_test")
        return ModelGenerator(self.manager, client, fixture_inputs, validate_fixture_segment)

    def test_plan_and_code_are_model_proposals_and_dry_run_has_no_actions(self):
        generator = self.generator()
        generator.create_plan()
        proposal = generator.generate_code()
        self.assertEqual(proposal.subgoal_id, "grasp")
        self.assertEqual(self.store.usage()["executions"], 0)
        self.assertEqual(self.backend.calls, [])
        self.assertIn("inputs", self.contexts[-1])
        self.assertIn("output_schema", self.contexts[-1])

    def test_invalid_plan_is_repaired_before_commit(self):
        def mutate(context, data):
            if not context["repair_feedback"]:
                data["goal_coverage"] = {}
            return data
        self.generator(mutate).create_plan()
        self.assertEqual(len(self.contexts), 2)
        self.assertIn("error", self.contexts[-1]["repair_feedback"])
        self.assertEqual(sum(e["kind"] == "PlanCreated" for e in self.store.events()), 1)

    def test_unsafe_code_repair_does_not_consume_attempt(self):
        self.scripted = ScriptedTransport("code_repair")
        generator = self.generator()
        generator.create_plan()
        report = generator.execute_next()
        self.assertEqual(report.status, "completed")
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].attempt_count, 1)
        self.assertEqual(self.store.usage()["executions"], 1)
        self.assertEqual(len(self.contexts), 3)

    def test_code_unknown_api_repaired(self):
        def mutate(context, data):
            if context["purpose"] == "code" and not context["repair_feedback"]:
                data["code"] = "pick_red_cube()"
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        self.assertEqual(generator.execute_next().status, "completed")
        self.assertNotIn("pick_red_cube", self.backend.calls)

    def test_code_cannot_write_runtime_progress_or_raise_budget(self):
        def mutate(context, data):
            if context["purpose"] == "code":
                data["task_status"] = "succeeded"
                data["budget"] = {"max_api_calls": 10000}
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        with self.assertRaisesRegex(ModelFault, "REPAIR_EXHAUSTED"):
            generator.execute_next()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.manager.read().progress.task_status, "active")

    def test_entry_conditions_cannot_be_weakened(self):
        def mutate(context, data):
            if context["purpose"] == "code":
                data["entry_conditions"] = []
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        with self.assertRaises(ModelFault):
            generator.execute_next()
        self.assertEqual(self.store.usage()["executions"], 0)

    def test_stale_model_state_refreshed_before_code_dispatch(self):
        changed = False
        def mutate(context, data):
            nonlocal changed
            if context["purpose"] == "code" and not changed:
                self.backend.version += 1
                changed = True
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        report = generator.execute_next()
        self.assertEqual(report.state_version_before, 1)
        self.assertEqual(len([c for c in self.contexts if c["purpose"] == "code"]), 2)
        self.assertEqual(self.store.usage()["executions"], 1)

    def test_wrong_plan_version_and_subgoal_are_rejected(self):
        def mutate(context, data):
            if context["purpose"] == "code":
                data["plan_version"] = 999
                data["subgoal_id"] = "place"
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        with self.assertRaises(ModelFault):
            generator.execute_next()
        self.assertEqual(self.backend.calls, [])

    def test_missing_input_reference_can_be_repaired_before_dispatch(self):
        def mutate(context, data):
            if context["purpose"] == "code" and not context["repair_feedback"]:
                data["code"] = "move_to_joints(INPUTS['missing_geometry'])"
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        self.assertEqual(generator.execute_next().status, "completed")
        self.assertEqual(self.store.usage()["executions"], 1)
        self.assertIn("missing_geometry", self.contexts[-1]["repair_feedback"]["error"])

    def test_model_result_success_text_does_not_override_verification(self):
        self.backend.planning_scenario = "budget_exhausted"
        def mutate(context, data):
            if context["purpose"] == "code":
                data["code"] += "RESULT = {'task_status': 'succeeded'}\nprint('SUCCESS')"
            return data
        generator = self.generator(mutate)
        summary = run_model_loop(self.manager, generator.client, fixture_inputs, validate_fixture_segment)
        self.assertEqual(summary["task_status"], "budget_exhausted")
        self.assertIsNone(self.manager.read().progress.final_verification_ref)

    def test_phase_boundaries_cannot_be_combined(self):
        def mutate(context, data):
            if context["purpose"] == "code":
                data["code"] += "move_to_joints(INPUTS['transport'])\nopen_gripper()\n"
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        with self.assertRaises(ModelFault):
            generator.execute_next()
        self.assertEqual(self.backend.calls, [])

    def test_invented_targets_rejected_before_action(self):
        def mutate(context, data):
            if context["purpose"] == "code":
                data["code"] = "open_gripper()\nclose_gripper()\nmove_to_joints([1, 2, 3, 0, 0, 0, 0])"
            return data
        generator = self.generator(mutate)
        generator.create_plan()
        with self.assertRaises(ModelFault):
            generator.execute_next()
        self.assertEqual(self.backend.calls, [])

    def test_execution_error_never_triggers_code_repair_or_replay(self):
        generator = self.generator()
        self.backend.scenario = "partial_error"
        summary = run_model_loop(self.manager, generator.client, fixture_inputs, validate_fixture_segment)
        self.assertEqual(summary["task_status"], "blocked")
        self.assertEqual(summary["executions"], 1)
        self.assertEqual(summary["model_calls"], 2)

    def test_patch_repair_preserves_history_and_recovery_budget(self):
        self.backend.planning_scenario = "drop_recovery"
        def mutate(context, data):
            if context["purpose"] == "patch" and not context["repair_feedback"]:
                data["add_subgoals"][0]["recovery_group_id"] = "reset-budget"
            return data
        generator = self.generator(mutate)
        summary = run_model_loop(self.manager, generator.client, fixture_inputs, validate_fixture_segment)
        self.assertEqual(summary["task_status"], "succeeded")
        self.assertEqual(summary["replans"], 1)
        current = self.manager.read()
        self.assertEqual(current.progress.subgoals["grasp"].status, "succeeded")
        self.assertEqual(current.progress.recovery_attempts["grasp"], 2)
        self.assertEqual(len([c for c in self.contexts if c["purpose"] == "patch"]), 2)

    def test_initial_failure_and_budget_failure_have_replayable_summaries(self):
        generator = self.generator(limits=ModelLimits(max_calls=1))
        summary = run_model_loop(self.manager, generator.client, fixture_inputs, validate_fixture_segment)
        self.assertEqual(summary["task_status"], "budget_exhausted")
        self.assertEqual(summary["executions"], 0)
        self.assertEqual(replay(self.temp.name)["model_summary"]["stop_reason"], "MODEL_BUDGET_EXHAUSTED")


class EndToEndTests(unittest.TestCase):
    def test_normal_retry_drop_and_code_repair(self):
        for scenario, executions, revisions in [("normal", 3, 0), ("grasp_retry", 4, 0),
                                                 ("drop_recovery", 5, 1), ("code_repair", 3, 0)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                summary = run_model_test_demo(directory, scenario)
                self.assertEqual((summary["task_status"], summary["executions"], summary["replans"]),
                                 ("succeeded", executions, revisions))
                result = replay(directory)
                self.assertTrue(all(r["execution_mode"] == "process" for r in result["reports"]))
                self.assertIsNotNone(result["progress"]["final_verification_ref"])

    def test_invalid_forever_has_no_plan_actions_or_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_model_test_demo(directory, "invalid_forever")
            self.assertEqual(summary["task_status"], "failed")
            self.assertEqual(summary["executions"], 0)
            self.assertEqual(summary["model_calls"], 3)
            result = replay(directory)
            self.assertNotIn("plan", result)
            self.assertEqual(result["model_summary"]["stop_reason"], "MODEL_REPAIR_EXHAUSTED")

    def test_unknown_stop_blocks_further_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_model_test_demo(directory, "stop_unknown")
            self.assertEqual(summary["task_status"], "blocked")
            self.assertEqual(summary["executions"], 2)
            self.assertEqual(summary["model_calls"], 3)
            self.assertEqual(replay(directory)["reports"][-1]["status"], "outcome_unknown")

    def test_recovery_budget_and_already_satisfied(self):
        for scenario, status, executions in [("budget_exhausted", "budget_exhausted", 3),
                                             ("already_satisfied", "succeeded", 0)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                summary = run_model_test_demo(directory, scenario)
                self.assertEqual((summary["task_status"], summary["executions"]), (status, executions))
