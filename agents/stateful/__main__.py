"""Lightweight CLI; no model, GPU, Gymnasium or simulator import is required."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import ExecutionReport, ExecutionRequest, FrameManifest, StateView, TaskSpec
from .demo import LIFT_SCRIPT, run_demo
from .execution.adapters.fake_backend import SCENARIOS
from .execution.store import replay
from .planning.contracts import (
    PlanPatch, PlanProposal, SegmentContract, TaskPlan, TaskProgress, VerificationReport, VerificationRequest,
)
from .planning.fixtures import SCENARIOS as PLANNING_SCENARIOS
from .models.contracts import CodeProposal, ModelConfig, ModelLimits


def main() -> int:
    parser = argparse.ArgumentParser(description="Cap-X stateful execution, planning and model proposals")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="run deterministic mock fixtures")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="normal")
    demo.add_argument("--script", type=Path, help="optional trusted straight-line Python script")
    planning = commands.add_parser("plan-demo", help="run a scripted planning/progress loop with mock feedback")
    planning.add_argument("--output", type=Path, required=True)
    planning.add_argument("--scenario", choices=(*PLANNING_SCENARIOS, "all"), default="normal")
    model = commands.add_parser("model-run", help="call a configured model with the synthetic stacking backend")
    model.add_argument("--config", type=Path, required=True)
    model.add_argument("--limits", type=Path, help="optional ModelLimits JSON")
    model.add_argument("--output", type=Path, required=True)
    model.add_argument("--scenario", choices=("normal", "grasp_retry", "drop_recovery"), default="normal")
    model_test = commands.add_parser("model-test-demo", help="offline scripted model replies through the model/worker pipeline")
    model_test.add_argument("--output", type=Path, required=True)
    model_test.add_argument("--scenario", default="normal")
    loop = commands.add_parser("closed-loop-demo", help="offline CoF/P/verification integration with fault injection")
    loop.add_argument("--output", type=Path, required=True)
    loop.add_argument("--scenario", default="normal")
    live_loop = commands.add_parser("closed-loop-run", help="configured model with CoF/P adapters and synthetic backend")
    live_loop.add_argument("--output", type=Path, required=True)
    live_loop.add_argument("--config", type=Path, required=True)
    live_loop.add_argument("--cof-mode", choices=("sensors", "frames"), default="sensors")
    live_loop.add_argument("--scenario", choices=("normal", "grasp_retry", "drop_recovery"), default="normal")
    playback = commands.add_parser("replay", help="read the ledger without executing any actions")
    playback.add_argument("directory", type=Path)
    schema = commands.add_parser("schema", help="export the shared JSON contracts")
    schema.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in {"closed-loop-demo", "closed-loop-run"}:
            from .feedback.demo import SCENARIOS as LOOP_SCENARIOS, run_closed_loop
            from .models.client import strict_json

            if args.command == "closed-loop-run":
                output = run_closed_loop(args.output, scenario=args.scenario, cof_mode=args.cof_mode,
                                         config=ModelConfig.model_validate(strict_json(args.config.read_text())))
            else:
                scenarios = LOOP_SCENARIOS if args.scenario == "all" else (args.scenario,)
                output = [run_closed_loop(args.output / scenario if args.scenario == "all" else args.output,
                                          scenario=scenario) for scenario in scenarios]
        elif args.command == "model-run":
            from .models.client import strict_json
            from .models.runner import run_model_fixture

            config = ModelConfig.model_validate(strict_json(args.config.read_text()))
            limits = ModelLimits.model_validate(strict_json(args.limits.read_text())) if args.limits else None
            output = run_model_fixture(args.output, config, limits=limits, scenario=args.scenario)
        elif args.command == "model-test-demo":
            from .models.fixtures import SCENARIOS as MODEL_SCENARIOS, run_model_test_demo

            scenarios = MODEL_SCENARIOS if args.scenario == "all" else (args.scenario,)
            output = [run_model_test_demo(args.output / scenario if args.scenario == "all" else args.output,
                                         scenario) for scenario in scenarios]
        elif args.command == "plan-demo":
            from .planning.demo import run_planning_demo

            scenarios = PLANNING_SCENARIOS if args.scenario == "all" else (args.scenario,)
            output = [run_planning_demo(args.output / scenario if args.scenario == "all" else args.output,
                                        scenario) for scenario in scenarios]
        elif args.command == "demo":
            code = args.script.read_text() if args.script else LIFT_SCRIPT
            scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
            output = [run_demo(args.output / scenario if args.scenario == "all" else args.output,
                               scenario, code) for scenario in scenarios]
        elif args.command == "replay":
            output = replay(args.directory)
        else:
            from .feedback.contracts import CoFRequest, CoFFeedback, CoFProposal, FeedbackLimits, AnalysisQuery
            from .state.contracts import PUpdateRequest, PStateAck, StateLimits

            args.output.mkdir(parents=True, exist_ok=True)
            for model in (TaskSpec, StateView, ExecutionRequest, ExecutionReport, FrameManifest,
                          PlanProposal, TaskPlan, TaskProgress, PlanPatch, SegmentContract,
                          VerificationRequest, VerificationReport, ModelConfig, ModelLimits, CodeProposal,
                          CoFRequest, CoFFeedback, CoFProposal, FeedbackLimits, AnalysisQuery,
                          PUpdateRequest, PStateAck, StateLimits):
                (args.output / f"{model.__name__}.json").write_text(
                    json.dumps(model.model_json_schema(), indent=2) + "\n")
            output = {"schema_directory": str(args.output.resolve())}
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
    print(json.dumps(output, indent=2, ensure_ascii=False))
    if args.command in {"model-run", "closed-loop-run"} and output["task_status"] != "succeeded":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
