"""Model-driven loop. The executable CLI currently uses a synthetic robot backend."""

from __future__ import annotations

from pathlib import Path

from ..execution.executor import Executor
from ..execution.backend import ExecutionFault
from ..execution.store import EventStore
from ..execution.worker import Interpreter
from ..planning.fixtures import StackingBackend, predicates, stack_task
from ..planning.manager import PlanningManager
from ..planning.progress import restore
from .client import ModelClient, ModelFault
from .contracts import ModelConfig, ModelLimits
from .generator import ModelGenerator


def fixture_inputs(state, node):
    """Trusted synthetic targets, not measured geometry or a motion planner."""
    return {"approach": [0.1, 0., 0., 0., 0., 0., 0.],
            "lift": [0.2, 0., 0., 0., 0., 0., 0.],
            "transport": [0.4, 0., 0., 0., 0., 0., 0.]}


def validate_fixture_segment(proposal, node, state, inputs):
    """Synthetic stacking policy: plan three separate phases, with dependencies.

    Grasp/lift success predicates: holding and lifted. Allowed sequence is
    open_gripper(), optional move_to_joints(INPUTS['approach']), close_gripper(),
    move_to_joints(INPUTS['lift']). A close-only segment is also allowed; a
    lift-only continuation requires an explicit holding entry condition.
    Transport success predicates: holding and above_for_placement. Its only
    action is move_to_joints(INPUTS['transport']).
    Release success predicates: on and gripper_open. Its only action is open_gripper().
    Each phase must return for verification before the next phase starts.
    Recovery nodes retain the original phase's conditions, group and API scope.

    This is intentionally a fixture policy. Real integrations must provide their
    own reviewed segment and grounded-argument validator.
    """
    calls = []

    def call(name, args, kwargs):
        if name == "move_to_joints":
            if kwargs:
                if args or set(kwargs) != {"joints"}:
                    raise ExecutionFault("ARGUMENT_INVALID")
                args = [kwargs["joints"]]
            if len(args) != 1:
                raise ExecutionFault("ARGUMENT_INVALID")
            target = next((key for key, value in inputs.items() if value == args[0]), None)
            if target is None:
                raise ExecutionFault("UNGROUNDED_TARGET", "use the supplied fixture targets")
            calls.append(target)
        else:
            if args or kwargs:
                raise ExecutionFault("ARGUMENT_INVALID")
            calls.append(name)

    Interpreter(inputs, node.candidate_skills, call).run(proposal.code)
    predicates_used = {c.predicate for c in node.success_conditions}
    allowed = []
    if predicates_used == {"holding", "lifted"}:
        allowed = [["open_gripper", "close_gripper", "lift"],
                   ["open_gripper", "approach", "close_gripper", "lift"], ["close_gripper"]]
        if any(c.predicate == "holding" and c.expected for c in proposal.entry_conditions):
            allowed.append(["lift"])
    elif predicates_used == {"holding", "above_for_placement"}:
        allowed = [["transport"]]
    elif predicates_used == {"on", "gripper_open"}:
        allowed = [["open_gripper"]]
    if calls not in allowed:
        raise ExecutionFault("SEGMENT_BOUNDARY_INVALID", "keep grasp/lift, transport and release in separate verified segments")


def run_model_loop(manager: PlanningManager, client: ModelClient, inputs_provider, segment_validator,
                   *, coordinator=None) -> dict:
    """Reusable interface for a backend with trusted state, predicates and grounded inputs."""
    state_provider = coordinator.observe_current if coordinator else lambda: manager.executor.backend.observe().state
    generator = ModelGenerator(manager, client, inputs_provider, segment_validator, state_provider=state_provider)
    store = manager.store
    status, reason = "failed", None
    try:
        generator.create_plan()
        state = state_provider()
        for _ in range(client.limits.max_loop_steps):
            decision = manager.next(state)
            store.append("PlannerDecision", manager.task.episode_id, decision.model_dump(mode="json"))
            if decision.kind == "stopped":
                break
            if decision.kind in {"execute_subgoal", "continue_subgoal"}:
                report = generator.execute_next()
                state = coordinator.after_execution(report) if coordinator else state_provider()
            elif decision.kind == "verify_subgoal":
                manager.verify_now(state, decision.subgoal_id)
            elif decision.kind == "verify_final":
                if coordinator:
                    state = state_provider()
                manager.verify_now(state)
            elif decision.kind == "request_observation":
                state = manager.observe()
            elif decision.kind == "revise_plan":
                generator.revise_plan()
                state = state_provider()
            elif decision.kind == "wait_for_execution":
                manager.terminate("blocked", "backend outcome unknown; explicit reconciliation required")
        else:
            raise ModelFault("MODEL_LOOP_BUDGET_EXHAUSTED")
    except (Exception, KeyboardInterrupt) as exc:
        reason = getattr(exc, "reason", "MODEL_RUN_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "MODEL_RUN_ERROR")
        status = ("budget_exhausted" if "BUDGET" in reason else
                  "interrupted" if isinstance(exc, KeyboardInterrupt) else "blocked" if store.pending() or
                  reason.startswith(("P_", "COF_", "FEEDBACK_")) else "failed")
        if restore(store).plan:
            manager.terminate(status, reason)
    finally:
        current = restore(store)
        if current.progress:
            status, reason = current.progress.task_status, current.progress.stop_reason
        summary = {"setting": client.setting, "backend_mode": manager.executor.backend.profile.mode,
                   "execution_mode": manager.executor.execution_mode,
                   "task_status": status, "stop_reason": reason,
                   "plan_version": current.plan.plan_version if current.plan else None,
                   "replans": current.progress.replans if current.progress else 0,
                   **store.usage(), **client.usage(), "run_directory": str(store.directory)}
        if coordinator:
            ack = coordinator.current()
            summary.update(closed_loop=True, cof_analyzer=coordinator.feedback.analyzer.name,
                           cof_reports=sum(e["kind"] == "CoFFeedbackProduced" for e in store.events()),
                           p_updates=sum(e["kind"] == "PUpdateApplied" for e in store.events()),
                           p_snapshot_id=ack.snapshot_id if ack else None,
                           processed_feedback_id=ack.processed_feedback_id if ack else None)
        store.append("ModelRunFinished", manager.task.episode_id, summary)
        store.write_json("models/summary.json", summary)
        manager.export()
        store.export()
    return summary


def run_model_fixture(directory: str | Path, config: ModelConfig, *, scenario="normal",
                      limits: ModelLimits | None = None, transport=None, setting="live_model_mock_backend"):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("model runs need an empty output directory; use replay for existing runs")
    task = stack_task(f"model-{scenario}")
    backend = StackingBackend(task.episode_id, scenario)
    with EventStore(directory) as store:
        manager = PlanningManager(Executor(task, backend, store, execution_mode="process"), predicates())
        client = ModelClient(config, store, limits, transport=transport, setting=setting)
        return run_model_loop(manager, client, fixture_inputs, validate_fixture_segment)
