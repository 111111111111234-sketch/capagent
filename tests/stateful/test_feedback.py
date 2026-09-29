"""Evidence and CoF tests use captured synthetic measurements, never a real VLM."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from capx.agents.stateful.contracts import Condition, StateView, canonical_json
from capx.agents.stateful.execution.backend import ExecutionFault
from capx.agents.stateful.execution.executor import Executor
from capx.agents.stateful.execution.store import EventStore, replay
from capx.agents.stateful.feedback.analyzers import ModelFrameAnalyzer, SensorTimelineAnalyzer
from capx.agents.stateful.feedback.contracts import AnalysisQuery, CoFProposal, EvidenceClaim, FeedbackLimits
from capx.agents.stateful.feedback.evidence import build_request, read_evidence
from capx.agents.stateful.feedback.service import assess, validate_proposal
from capx.agents.stateful.models.client import HTTPReply, ModelClient
from capx.agents.stateful.models.contracts import ModelConfig
from capx.agents.stateful.planning.fixtures import FixedPlanner, HOLDING, StackingBackend, predicates, stack_task
from capx.agents.stateful.planning.manager import PlanningManager
from capx.agents.stateful.state.coordinator import StateCoordinator


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EventStore(self.root)
        self.task = stack_task("feedback-test")
        self.backend = StackingBackend(self.task.episode_id)
        self.manager = PlanningManager(Executor(self.task, self.backend, self.store, execution_mode="process"), predicates())
        self.coordinator = StateCoordinator(self.manager, SensorTimelineAnalyzer())
        state = self.coordinator.observe_current()
        plan = self.manager.create_plan(FixedPlanner().create_plan(state), state)
        code, inputs = FixedPlanner().code_for(plan.subgoals[0], 0)
        self.report = self.manager.execute_segment("grasp", state, code=code, inputs=inputs)
        self.request = build_request(self.manager, self.report, FeedbackLimits())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_captures_have_hashes_global_ids_and_api_boundaries(self):
        frames = [e for e in self.request.evidence if e.kind == "frame"]
        self.assertTrue(frames)
        self.assertTrue(all(e.sha256 and e.evidence_id.startswith("exec-1.") for e in frames))
        self.assertTrue(any(e.call_id == "call-1" for e in frames))
        self.assertEqual(frames[0].boundary, "before_execution")
        self.assertEqual(frames[-1].boundary, "after_execution")

    def test_sensor_timeline_reports_changes_and_terminal_conditions(self):
        request, feedback = self.coordinator.feedback.analyze(self.report)
        holding = [c for c in feedback.condition_evidence if c.condition_id == HOLDING.id]
        self.assertEqual(holding[0].assessment, "supported")
        self.assertTrue(any(e.predicate == "holding" and not e.before and e.after for e in feedback.events))
        self.assertTrue(all(e.evidence_refs for e in feedback.events))
        self.assertNotIn("task_status", feedback.model_dump())
        self.assertNotIn("SUCCESS", request.model_dump_json())

    def test_duplicate_analysis_is_cached_without_extra_usage(self):
        first = self.coordinator.feedback.analyze(self.report)
        calls = sum(e["kind"] == "CoFAnalysisRequested" for e in self.store.events())
        second = self.coordinator.feedback.analyze(self.report)
        self.assertEqual(first, second)
        self.assertEqual(calls, sum(e["kind"] == "CoFAnalysisRequested" for e in self.store.events()))

    def test_maintained_and_stable_queries_cannot_use_boundary_samples(self):
        queries = [AnalysisQuery(query_id=scope, condition=HOLDING, scope=scope)
                   for scope in ("maintained", "stable_for_window", "occurred")]
        _, feedback = self.coordinator.feedback.analyze(self.report, queries)
        checks = {c.query_id: c.assessment for c in feedback.condition_evidence}
        self.assertEqual(checks["maintained"], "unknown")
        self.assertEqual(checks["stable_for_window"], "unknown")
        self.assertEqual(checks["occurred"], "supported")

    def test_negated_unknown_is_not_supported(self):
        condition = Condition(id="not-holding", predicate="holding", args=["robot", "red_cube"], expected=False)
        request = self.request.model_copy(update={"queries": [AnalysisQuery(query_id="negative", condition=condition)]})
        _, checks, _ = assess(request, CoFProposal())
        self.assertEqual(checks[0].assessment, "unknown")

    def test_pre_execution_claim_is_not_terminal_evidence(self):
        proposal = SensorTimelineAnalyzer().analyze(self.request)
        before_ids = {e.evidence_id for e in self.request.evidence if e.boundary == "before_execution"}
        proposal = CoFProposal(claims=[c for c in proposal.claims if set(c.evidence_refs) <= before_ids])
        terminal, checks, _ = assess(self.request, proposal)
        self.assertFalse(terminal)
        self.assertTrue(all(c.assessment == "unknown" for c in checks))

    def test_fabricated_reference_and_object_are_rejected(self):
        claim = SensorTimelineAnalyzer().analyze(self.request).claims[0]
        for changes in [{"evidence_refs": ["another-execution.frame-1"]}, {"args": ["imaginary"]}]:
            with self.subTest(changes=changes), self.assertRaises(ExecutionFault):
                validate_proposal(CoFProposal(claims=[claim.model_copy(update=changes)]), self.request)

    def test_sensor_claim_cannot_lie_about_its_raw_evidence(self):
        claim = next(c for c in SensorTimelineAnalyzer().analyze(self.request).claims if c.value is not None)
        with self.assertRaisesRegex(ExecutionFault, "COF_SENSOR_CLAIM_MISMATCH"):
            validate_proposal(CoFProposal(claims=[claim.model_copy(update={"value": not claim.value})]), self.request)

    def test_frame_claim_must_use_acquisition_time_and_known_clock(self):
        frame = next(e for e in self.request.evidence if e.kind == "frame")
        claim = EvidenceClaim(claim_id="visual", predicate="holding", args=HOLDING.args, value=True,
                               observed_at="2000-01-01T00:00:00+00:00", evidence_refs=[frame.evidence_id])
        with self.assertRaisesRegex(ExecutionFault, "COF_CLAIM_TIME_MISMATCH"):
            validate_proposal(CoFProposal(claims=[claim]), self.request)
        with self.assertRaisesRegex(ExecutionFault, "COF_CLOCK_MISMATCH"):
            validate_proposal(CoFProposal(claims=[claim.model_copy(update={"observed_at": "2000-01-01T00:00:00"})]), self.request)

    def test_evidence_reader_rejects_traversal_and_hash_change(self):
        with self.assertRaises(ExecutionFault):
            read_evidence(self.store, "../outside", limit=100)
        frame = next(e for e in self.request.evidence if e.kind == "frame")
        (self.root / frame.path).write_bytes(b"changed")
        with self.assertRaisesRegex(ExecutionFault, "COF_EVIDENCE_HASH_MISMATCH"):
            read_evidence(self.store, frame.path, limit=100, sha256=frame.sha256)

    def test_corrupt_final_state_cannot_support_terminal_claims(self):
        (self.root / self.report.state_after_ref).write_text("{}")
        request, feedback = self.coordinator.feedback.analyze(self.report)
        self.assertTrue(request.gaps)
        self.assertFalse(feedback.terminal_claims)
        self.assertTrue(all(c.assessment == "unknown" for c in feedback.condition_evidence))

    def test_missing_images_are_reported_even_when_sensors_prove_conditions(self):
        for frame in self.request.evidence:
            if frame.kind == "frame":
                (self.root / frame.path).unlink()
        _, feedback = self.coordinator.feedback.analyze(self.report)
        self.assertEqual(feedback.analysis_status, "partial")
        self.assertEqual(feedback.selected_frames, 0)
        self.assertTrue(any(c.assessment == "supported" for c in feedback.condition_evidence))

    def test_selection_is_bounded_and_keeps_endpoints(self):
        request = build_request(self.manager, self.report, FeedbackLimits(max_frames=2, max_state_records=2))
        frames = [e for e in request.evidence if e.kind == "frame"]
        self.assertEqual([e.boundary for e in frames], ["before_execution", "after_execution"])
        self.assertEqual(len([e for e in request.evidence if e.state]), 2)
        self.assertTrue(request.gaps)

    def test_bad_analyzer_output_has_finite_repair_and_no_success_fallback(self):
        analyzer = Mock(name="bad-analyzer")
        analyzer.name = "bad-fixture"
        analyzer.analyze.return_value = CoFProposal(claims=[EvidenceClaim(claim_id="bad", predicate="holding",
             args=HOLDING.args, value=True, observed_at=self.request.evidence[0].observed_at, evidence_refs=["invented"])])
        self.coordinator.feedback.analyzer = analyzer
        _, feedback = self.coordinator.feedback.analyze(self.report)
        self.assertEqual(analyzer.analyze.call_count, 2)
        self.assertEqual(feedback.analysis_status, "unavailable")
        self.assertFalse(feedback.claims)
        self.assertTrue(all(c.assessment == "unknown" for c in feedback.condition_evidence))

    def test_frame_model_receives_ordered_images_and_schema_but_no_result(self):
        captured = []
        def transport(endpoint, headers, body, timeout, limit):
            captured.append(json.loads(body))
            content = canonical_json({"claims": []})
            return HTTPReply(200, canonical_json({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode())
        client = ModelClient(ModelConfig(model="test-vision", endpoint="http://127.0.0.1/unused"),
                             self.store, transport=transport, setting="scripted-vision-protocol-test")
        output = ModelFrameAnalyzer(client).analyze(self.request)
        self.assertFalse(output.claims)
        content = captured[0]["messages"][1]["content"]
        images = [x for x in content if x["type"] == "image_url"]
        self.assertEqual(len(images), len([e for e in self.request.evidence if e.kind == "frame"]))
        data = base64.b64decode(images[0]["image_url"]["url"].split(",")[1])
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertNotIn("runtime_rc", content[0]["text"])
        self.assertNotIn("goal_conditions", content[0]["text"])
        self.assertEqual(client.usage()["model_calls"], 1)

    def test_same_time_camera_claims_are_not_a_temporal_transition(self):
        frame = next(e for e in self.request.evidence if e.kind == "frame")
        claims = [EvidenceClaim(claim_id=f"view-{i}", predicate="holding", args=HOLDING.args, value=value,
                              observed_at=frame.observed_at, evidence_refs=[frame.evidence_id]) for i, value in enumerate((True, False))]
        _, _, events = assess(self.request, CoFProposal(claims=claims))
        self.assertFalse(events)

    def test_different_cameras_do_not_form_a_fake_temporal_transition(self):
        frames = [e for e in self.request.evidence if e.kind == "frame"]
        first, last = frames[0], frames[-1].model_copy(update={"camera_id": "other-camera"})
        request = self.request.model_copy(update={"evidence": [first, last]})
        claims = [EvidenceClaim(claim_id=f"view-{i}", predicate="holding", args=HOLDING.args, value=value,
                               observed_at=record.observed_at, evidence_refs=[record.evidence_id])
                  for i, (record, value) in enumerate([(first, True), (last, False)])]
        _, _, events = assess(request, CoFProposal(claims=claims))
        self.assertFalse(events)

    def test_duplicate_claim_ids_are_rejected(self):
        claim = SensorTimelineAnalyzer().analyze(self.request).claims[0]
        with self.assertRaisesRegex(ExecutionFault, "COF_DUPLICATE_CLAIM"):
            validate_proposal(CoFProposal(claims=[claim, claim]), self.request)

    def test_frame_manifest_metadata_cannot_be_rewritten_after_capture(self):
        path = self.root / self.report.frame_manifest_ref
        data = json.loads(path.read_text())
        data["frames"][-1]["observed_at"] = "2099-01-01T00:00:00+00:00"
        path.write_text(canonical_json(data))
        with self.assertRaisesRegex(ExecutionFault, "COF_MANIFEST_HASH_MISMATCH"):
            build_request(self.manager, self.report, FeedbackLimits())

    def test_execution_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ExecutionFault, "COF_REPORT_MISMATCH"):
            build_request(self.manager, self.report.model_copy(update={"episode_id": "wrong"}), FeedbackLimits())

    def test_cof_does_not_advance_progress_before_p_and_verification(self):
        self.coordinator.feedback.analyze(self.report)
        self.assertEqual(self.manager.read().progress.subgoals["grasp"].status, "awaiting_verification")
        with self.assertRaises(ExecutionFault):
            self.manager.verify_now(self.backend.observe().state, "grasp")
