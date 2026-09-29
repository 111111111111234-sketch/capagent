"""Scripted JSON responses ONLY for offline protocol tests; never a live fallback."""

from ..contracts import StateView, canonical_json
from ..planning.contracts import Subgoal, TaskPlan, TaskProgress
from ..planning.fixtures import FixedPlanner
from ..planning.progress import PlanningState
from .client import HTTPReply, strict_json
from .contracts import ModelConfig
from .runner import run_model_fixture

SCENARIOS = ("normal", "grasp_retry", "drop_recovery", "code_repair", "invalid_forever",
             "already_satisfied", "budget_exhausted", "stop_unknown")


class ScriptedTransport:
    def __init__(self, scenario="normal"):
        self.scenario = scenario
        self.code_rejected = False

    def __call__(self, endpoint, headers, body, timeout_s, max_response_bytes):
        context = strict_json(strict_json(body.decode())["messages"][-1]["content"])
        state = StateView.model_validate(context["state"])
        purpose = context["purpose"]
        planner = FixedPlanner()
        if self.scenario == "invalid_forever":
            content = "not JSON"
        elif purpose == "plan":
            content = planner.create_plan(state).model_dump_json()
        elif purpose == "patch":
            current = PlanningState()
            current.plan = TaskPlan.model_validate(context["plan"])
            current.progress = TaskProgress.model_validate(context["progress"])
            content = planner.revise_plan(current, state).model_dump_json()
        else:
            node = Subgoal.model_validate(context["subgoal"])
            attempt = context["progress"]["subgoals"][node.id]["attempt_count"]
            code, _ = planner.code_for(node, attempt)
            code = code.replace("INPUTS['target']", "INPUTS['lift']" if node.recovery_group_id == "grasp" else "INPUTS['transport']")
            if self.scenario == "code_repair" and not self.code_rejected:
                code = "import os\n"
                self.code_rejected = True
            content = canonical_json({"based_on_state_version": state.state_version,
                                      "plan_version": context["plan"]["plan_version"], "subgoal_id": node.id,
                                      "intent": node.goal, "code": code,
                                      "entry_conditions": [c.model_dump() for c in node.preconditions],
                                      "expected_conditions": [c.model_dump() for c in node.success_conditions],
                                      "continue_conditions": []})
        # Usage deliberately omitted: scripted fixtures do not measure real model tokens.
        return HTTPReply(200, canonical_json({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode())


def run_model_test_demo(directory, scenario="normal"):
    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    backend_scenario = "normal" if scenario in {"code_repair", "invalid_forever"} else scenario
    return run_model_fixture(directory, ModelConfig(model="scripted-test-responses",
                             endpoint="http://127.0.0.1/unused-test-endpoint"), scenario=backend_scenario,
                             transport=ScriptedTransport(scenario), setting=f"scripted_model_mock_backend:{scenario}")
