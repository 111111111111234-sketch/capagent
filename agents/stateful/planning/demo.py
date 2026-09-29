"""Minimal deterministic coordination loop, separate from future model/CoF controllers."""

from __future__ import annotations

from pathlib import Path

from ..execution.executor import Executor
from ..execution.store import EventStore
from .fixtures import FixedPlanner, StackingBackend, predicates, stack_task
from .manager import PlanningManager


def run_planning_demo(directory: str | Path, scenario: str = "normal") -> dict:
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("planning demo needs an empty output directory; use replay for existing runs")
    task = stack_task(f"planning-{scenario}")
    backend, planner = StackingBackend(task.episode_id, scenario), FixedPlanner()
    occlusion_injected = disturbance_injected = False
    with EventStore(directory) as store:
        manager = PlanningManager(Executor(task, backend, store), predicates())
        state = backend.observe().state
        manager.create_plan(planner.create_plan(state), state)
        for _ in range(100):
            decision = manager.next(state)
            store.append("PlannerDecision", task.episode_id, decision.model_dump(mode="json"))
            if decision.kind == "stopped":
                break
            if decision.kind in {"execute_subgoal", "continue_subgoal"}:
                current = manager.read()
                node = next(node for node in current.plan.subgoals if node.id == decision.subgoal_id)
                code, inputs = planner.code_for(node, current.progress.subgoals[node.id].attempt_count)
                manager.execute_segment(node.id, state, code=code, inputs=inputs)
                if scenario in {"occlusion", "unknown_forever"} and not occlusion_injected:
                    backend.occluded = occlusion_injected = True
                state = backend.observe().state
            elif decision.kind == "verify_subgoal":
                manager.verify_now(state, decision.subgoal_id)
            elif decision.kind == "verify_final":
                if scenario == "final_disturbance" and not disturbance_injected:
                    backend.on = False
                    backend.version += 1
                    disturbance_injected = True
                    store.append("FixtureDisturbance", task.episode_id, {"effect": "support relation lost"})
                    state = backend.observe().state
                manager.verify_now(state)
            elif decision.kind == "request_observation":
                if scenario == "occlusion":
                    backend.occluded = False
                state = manager.observe()
            elif decision.kind == "revise_plan":
                manager.commit_patch(planner.revise_plan(manager.read(), state), state)
            elif decision.kind == "wait_for_execution":
                manager.terminate("interrupted", "backend outcome unknown; explicit reconciliation required")
        else:
            raise RuntimeError("fixture loop did not converge within its diagnostic limit")
        manager.export()
        store.export()
        current = manager.read()
        summary = {"scenario": scenario, "setting": "mock_scripted_planner",
                   "task_status": current.progress.task_status, "stop_reason": current.progress.stop_reason,
                   "plan_version": current.plan.plan_version, "replans": current.progress.replans,
                   "recovery_attempts": current.progress.recovery_attempts, **store.usage(),
                   "run_directory": str(store.directory)}
        store.write_json("planning/summary.json", summary)
        return summary
