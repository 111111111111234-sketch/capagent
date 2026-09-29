"""A scripted planner and synthetic stacking world for protocol tests only."""

from __future__ import annotations

from ..contracts import Condition, Fact, StateView, TaskSpec
from ..execution.adapters.fake_backend import FakeBackend
from ..execution.backend import Observation
from .contracts import PlanPatch, PlanProposal, PredicateCatalog, PredicateSpec, Subgoal
from .progress import PlanningState

SCENARIOS = (
    "normal", "grasp_retry", "drop_recovery", "occlusion", "unknown_forever",
    "budget_exhausted", "already_satisfied", "final_disturbance", "stop_unknown",
)


def condition(name: str, predicate: str, *args: str) -> Condition:
    return Condition(id=name, predicate=predicate, args=list(args))


HOLDING = condition("holding_red", "holding", "robot", "red_cube")
LIFTED = condition("lifted_red", "lifted", "red_cube")
ABOVE = condition("above_green", "above_for_placement", "red_cube", "green_cube")
ON = condition("on_green", "on", "red_cube", "green_cube")
OPEN = condition("gripper_open", "gripper_open", "robot")
POSE_RED = condition("red_located", "pose_known", "red_cube")


def predicates() -> PredicateCatalog:
    return PredicateCatalog(catalog_version="stack_fixture_v1", predicates=[
        PredicateSpec(name="holding", arity=2, description="fixture gripper contains the named object"),
        PredicateSpec(name="lifted", arity=1, description="fixture object is above its original support"),
        PredicateSpec(name="gripper_open", arity=1, description="fixture gripper is open"),
        PredicateSpec(name="pose_known", arity=1, description="fixture object position is available"),
        PredicateSpec(name="above_for_placement", arity=2, description="fixture transport reached the support"),
        PredicateSpec(name="on", arity=2, description="fixture release established support; no physical stability claim"),
    ])


def stack_task(episode_id: str) -> TaskSpec:
    return TaskSpec(episode_id=episode_id, task_id="stack_red_on_green",
                    instruction="Place the red cube on the green cube and release the gripper.",
                    goal_conditions=[ON, OPEN], constraints=["Synthetic fixture; serial single-arm execution."])


class FixedPlanner:
    """Returns explicit proposals and patches; it never writes runtime progress."""

    def create_plan(self, state: StateView) -> PlanProposal:
        return PlanProposal(based_on_state_version=state.state_version,
                            goal_coverage={ON.id: ["place"], OPEN.id: ["place"]}, subgoals=[
            Subgoal(id="grasp", goal="Grasp and lift the red cube", preconditions=[POSE_RED],
                    success_conditions=[HOLDING, LIFTED],
                    candidate_skills=["open_gripper", "close_gripper", "move_to_joints"], recovery_group_id="grasp"),
            Subgoal(id="transport", goal="Move the red cube above the green cube", depends_on=["grasp"],
                    preconditions=[HOLDING, LIFTED], success_conditions=[HOLDING, ABOVE],
                    candidate_skills=["move_to_joints"], recovery_group_id="transport"),
            Subgoal(id="place", goal="Release the red cube onto the green cube", depends_on=["transport"],
                    preconditions=[HOLDING, ABOVE], success_conditions=[ON, OPEN],
                    candidate_skills=["open_gripper"], recovery_group_id="place"),
        ])

    def revise_plan(self, current: PlanningState, state: StateView) -> PlanPatch:
        nodes = {node.id: node for node in current.plan.subgoals}
        suffix = current.plan.plan_version + 1
        grasp = nodes["grasp"].model_copy(update={
            "id": f"recover-grasp-{suffix}", "depends_on": ["grasp"], "recovery_of": "grasp"})
        transport = nodes["transport"].model_copy(update={
            "id": f"recover-transport-{suffix}", "depends_on": [grasp.id], "recovery_of": "transport"})
        updates, retire, coverage = {}, [], None
        added = [grasp, transport]
        if current.progress.subgoals["place"].status == "succeeded":
            place = nodes["place"].model_copy(update={
                "id": f"recover-place-{suffix}", "depends_on": [transport.id], "recovery_of": "place"})
            added.append(place)
            coverage = {ON.id: [place.id], OPEN.id: [place.id]}
        else:
            if current.progress.subgoals["transport"].status not in {"succeeded", "superseded"}:
                retire = ["transport"]
            updates = {"place": [transport.id]}
        return PlanPatch(base_plan_version=current.plan.plan_version,
                         base_progress_revision=current.progress.revision,
                         based_on_state_version=state.state_version, reason="fixture observed loss of support/holding",
                         retire_subgoals=retire, add_subgoals=added, dependency_updates=updates,
                         goal_coverage=coverage)

    def code_for(self, node: Subgoal, attempt_count: int) -> tuple[str, dict]:
        purpose = node.recovery_group_id
        if purpose == "grasp":
            # Retrying includes a new approach segment instead of blindly replaying the same program.
            approach = "move_to_joints(INPUTS['approach'])\n" if attempt_count or node.recovery_of else ""
            code = "open_gripper()\n" + approach + "close_gripper()\nmove_to_joints(INPUTS['target'])\n"
            return code, {"approach": [0.1, 0., 0., 0., 0., 0., 0.], "target": [0.2, 0., 0., 0., 0., 0., 0.]}
        if purpose == "transport":
            return "move_to_joints(INPUTS['target'])\n", {"target": [0.4, 0., 0., 0., 0., 0., 0.]}
        return "open_gripper()\n", {}


class StackingBackend(FakeBackend):
    def __init__(self, episode_id: str, scenario: str = "normal"):
        if scenario not in SCENARIOS:
            raise ValueError(scenario)
        super().__init__(episode_id)
        self.planning_scenario = scenario
        self.above = False
        self.on = scenario == "already_satisfied"
        self.grasp_attempts = 0
        self.drop_used = False
        self.occluded = False

    def close_gripper(self) -> None:
        super().close_gripper()
        self.grasp_attempts += 1
        self.on = False
        if self.planning_scenario == "budget_exhausted" or (self.planning_scenario == "grasp_retry" and self.grasp_attempts == 1):
            self.holding = False

    def open_gripper(self) -> None:
        if self.holding:
            self.on = self.above
        super().open_gripper()
        self.above = False

    def move_to_joints(self, joints: list[float]) -> None:
        super().move_to_joints(joints)
        if joints[0] == 0.4:
            self.above = self.holding
            if self.planning_scenario == "drop_recovery" and not self.drop_used:
                self.holding, self.lifted, self.above = False, False, False
                self.drop_used = True
            if self.planning_scenario == "stop_unknown":
                self.stop_available = False

    def observe(self) -> Observation:
        observation = super().observe()
        state = observation.state
        extra = [Fact(predicate=predicate, args=args, value=value, observed_at=state.observed_at,
                      source="mock", evidence_refs=[f"mock-sensor-v{self.version}"])
                 for predicate, args, value in (
                     ("pose_known", ["red_cube"], True), ("pose_known", ["green_cube"], True),
                     ("above_for_placement", ["red_cube", "green_cube"], self.above),
                     ("on", ["red_cube", "green_cube"], self.on),
                 )]
        facts = state.facts + extra
        if self.occluded:
            facts = [fact.model_copy(update={"value": None})
                     if fact.predicate in {"holding", "lifted"} else fact for fact in facts]
        observation.state = state.model_copy(update={"objects": ["robot", "red_cube", "green_cube"], "facts": facts})
        return observation
