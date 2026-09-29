"""Reproducible fixed-script fixtures and a separate mock-only evaluator."""

from __future__ import annotations

from pathlib import Path

from .contracts import Condition, ExecutionRequest, StateView, TaskSpec, utc_now
from .execution.adapters.fake_backend import FakeBackend
from .execution.executor import Executor
from .execution.store import EventStore

LIFT_SCRIPT = """close_gripper()
move_to_joints(INPUTS['lift_joints'])
RESULT = {'segment': 'lift_requested'}
print('Fixed lift script returned')
"""


def lift_task(episode_id: str) -> TaskSpec:
    return TaskSpec(
        episode_id=episode_id, task_id="red_cube_lift",
        instruction="Grasp the red cube and hold it above its starting support.",
        goal_conditions=[Condition(id="g_holding", predicate="holding", args=["robot", "red_cube"]),
                         Condition(id="g_lifted", predicate="lifted", args=["red_cube"])],
        constraints=["Single arm; fixed synthetic targets; mock fixture only."],
    )


def lift_request(episode_id: str, *, code: str = LIFT_SCRIPT) -> ExecutionRequest:
    return ExecutionRequest(
        episode_id=episode_id, execution_id="lift-1", subgoal_id="grasp-and-lift",
        attempt_id="attempt-1", segment_id="segment-1", catalog_version=FakeBackend.catalog_version,
        based_on_state_version=0, code=code, allowed_skills=["close_gripper", "move_to_joints"],
        inputs={"lift_joints": [0.1, 0.0, 0.0, -0.1, 0.0, 0.0, 0.0]},
        entry_conditions=[Condition(id="c_open", predicate="gripper_open", args=["robot"])],
    )


def evaluate_mock(task: TaskSpec, state: StateView | None) -> dict:
    """Fixture scoring only. Does not update execution reports or online progress."""
    checks = []
    facts = {(fact.predicate, tuple(fact.args)): fact for fact in state.facts} if state else {}
    for goal in task.goal_conditions:
        fact = facts.get((goal.predicate, tuple(goal.args)))
        if fact is None or fact.value is None or fact.source != "mock":
            verdict = "unknown"
        else:
            verdict = "pass" if fact.value == goal.expected else "fail"
        checks.append({"condition_id": goal.id, "verdict": verdict})
    verdicts = [check["verdict"] for check in checks]
    outcome = "fail" if "fail" in verdicts else "unscorable" if "unknown" in verdicts else "pass"
    return {"setting": "mock_oracle", "evaluated_at": utc_now(), "checks": checks,
            "evaluator_outcome": outcome, "writes_task_progress": False}


def run_demo(directory: str | Path, scenario: str = "normal", code: str = LIFT_SCRIPT) -> dict:
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("demo needs an empty output directory; use replay to inspect an existing run")
    task = lift_task(f"demo-{scenario}")
    backend = FakeBackend(task.episode_id, scenario)
    with EventStore(directory) as store:
        executor = Executor(task, backend, store)
        report = executor.execute(lift_request(task.episode_id, code=code))
        # Scoring requires an actual final capture. An old pre-execution state is not enough.
        captured_final = any(event["kind"] == "ObservationCaptured"
                             and event["payload"]["boundary"] == "after_execution"
                             for event in store.events(report.execution_id))
        state = None
        if report.state_after_ref and captured_final and report.backend_motion_state == "idle":
            state = StateView.model_validate_json((store.directory / report.state_after_ref).read_text())
        evaluation = evaluate_mock(task, state)
        store.write_json("evaluation.json", evaluation)
        store.export()
        summary = {"scenario": scenario, "setting": "mock", "execution_status": report.status,
                   "runtime_rc": report.runtime_rc, "evaluator_outcome": evaluation["evaluator_outcome"],
                   "api_calls": report.api_calls, "run_directory": str(store.directory)}
        store.write_json("summary.json", summary)
        return summary
