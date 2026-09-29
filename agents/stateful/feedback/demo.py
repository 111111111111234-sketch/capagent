"""Synthetic closed-loop integration, with replaceable frame-model and P adapters."""

from pathlib import Path

from ..execution.executor import Executor
from ..execution.store import EventStore
from ..models.client import ModelClient
from ..models.contracts import ModelConfig
from ..models.fixtures import ScriptedTransport
from ..models.runner import fixture_inputs, run_model_loop, validate_fixture_segment
from ..planning.fixtures import StackingBackend, predicates, stack_task
from ..planning.manager import PlanningManager
from ..state.coordinator import StateCoordinator
from ..state.provider import LocalPAdapter
from .analyzers import ModelFrameAnalyzer, SensorTimelineAnalyzer

SCENARIOS = ("normal", "grasp_retry", "drop_recovery", "occlusion", "unknown_forever", "missing_frames",
             "partial_error", "cancelled", "timed_out", "stop_unknown", "budget_exhausted",
             "already_satisfied", "final_disturbance", "p_delayed", "p_timeout")


class LoopFixtureBackend(StackingBackend):
    def __init__(self, episode_id, scenario):
        base = scenario if scenario in {"grasp_retry", "drop_recovery", "stop_unknown", "budget_exhausted", "already_satisfied"} else "normal"
        super().__init__(episode_id, base)
        self.loop_scenario, self.injected = scenario, False
        if scenario in {"partial_error", "cancelled", "timed_out"}:
            self.scenario = scenario

    def move_to_joints(self, joints):
        super().move_to_joints(joints)
        if self.loop_scenario in {"occlusion", "unknown_forever"} and not self.injected:
            self.occluded = self.injected = True

    def observe(self):
        observation = super().observe()
        if self.loop_scenario == "missing_frames":
            observation.frames = []
        return observation


class DemoCoordinator(StateCoordinator):
    def observe_current(self):
        backend = self.manager.executor.backend
        if self.latest_report():
            if backend.loop_scenario == "occlusion":
                backend.occluded = False
            if backend.loop_scenario == "final_disturbance" and not backend.injected:
                current = self.manager.read()
                if all(item.status == "succeeded" for item in current.progress.subgoals.values()):
                    backend.on = False
                    backend.version += 1
                    backend.injected = True
                    self.store.append("FixtureDisturbance", self.manager.task.episode_id, {"effect": "support relation lost"})
        return super().observe_current()


class DelayedPAdapter(LocalPAdapter):
    name = "scripted-delayed-p-v1"

    def __init__(self, forever=False):
        self.forever, self.seen = forever, set()

    def update(self, request, previous, *, timeout_s):
        if request.feedback and (self.forever or request.update_id not in self.seen):
            self.seen.add(request.update_id)
            return None
        return super().update(request, previous, timeout_s=timeout_s)


def run_closed_loop(directory, *, scenario="normal", config=None, cof_mode="sensors", transport=None):
    if scenario not in SCENARIOS or cof_mode not in {"sensors", "frames"}:
        raise ValueError("unknown closed-loop scenario or feedback mode")
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("closed-loop runs need an empty output directory; use replay for existing records")
    scripted = config is None
    if scripted and cof_mode != "sensors":
        raise ValueError("frame-model mode requires an explicit model configuration")
    config = config or ModelConfig(model="scripted-test-responses", endpoint="http://127.0.0.1/unused-test-endpoint")
    transport = transport or (ScriptedTransport() if scripted else None)
    task = stack_task(f"loop-{scenario}")
    backend = LoopFixtureBackend(task.episode_id, scenario)
    with EventStore(directory) as store:
        manager = PlanningManager(Executor(task, backend, store, execution_mode="process"), predicates())
        client = ModelClient(config, store, transport=transport,
                             setting="closed_loop_scripted_mock" if scripted else "closed_loop_live_model_mock_backend")
        analyzer = SensorTimelineAnalyzer() if cof_mode == "sensors" else ModelFrameAnalyzer(client)
        adapter = DelayedPAdapter(scenario == "p_timeout") if scenario in {"p_delayed", "p_timeout"} else None
        coordinator = DemoCoordinator(manager, analyzer, adapter=adapter)
        summary = run_model_loop(manager, client, fixture_inputs, validate_fixture_segment, coordinator=coordinator)
        summary["scenario"] = scenario
        store.write_json("closed-loop-summary.json", summary)
        return summary
