"""Graph/condition validation shared by proposals, patches and execution contracts."""

from __future__ import annotations

from ..contracts import Condition, SkillCatalog, StateView, TaskSpec
from ..execution.backend import ExecutionFault
from .contracts import PlanProposal, PredicateCatalog


def purpose_key(conditions: list[Condition]) -> frozenset:
    """A renamed condition ID must not create a new retry budget for the same goal."""
    return frozenset((condition.predicate, tuple(condition.args), condition.expected) for condition in conditions)


def validate_conditions(conditions: list[Condition], state: StateView, predicates: PredicateCatalog,
                        definitions: dict[str, Condition] | None = None) -> None:
    definitions = definitions if definitions is not None else {}
    arities = {p.name: p.arity for p in predicates.predicates}
    for condition in conditions:
        if condition.predicate not in arities or len(condition.args) != arities[condition.predicate]:
            raise ExecutionFault("INVALID_CONDITION", f"unknown predicate or wrong arity: {condition.id}")
        if not set(condition.args) <= set(state.objects):
            raise ExecutionFault("INVALID_CONDITION", f"unknown object: {condition.id}")
        if condition.id in definitions and definitions[condition.id] != condition:
            raise ExecutionFault("CONDITION_CONFLICT", condition.id)
        definitions[condition.id] = condition


def validate_plan(proposal: PlanProposal, task: TaskSpec, state: StateView,
                  skills: SkillCatalog, predicates: PredicateCatalog, max_subgoals: int,
                  retired: set[str] | None = None) -> None:
    retired = retired or set()
    if state.episode_id != task.episode_id or proposal.based_on_state_version != state.state_version:
        raise ExecutionFault("STATE_STALE")
    nodes = {node.id: node for node in proposal.subgoals}
    if len(nodes) != len(proposal.subgoals) or len(nodes) > max_subgoals:
        raise ExecutionFault("INVALID_PLAN", "duplicate nodes or plan exceeds node budget")
    if not retired <= nodes.keys():
        raise ExecutionFault("INVALID_PLAN", "unknown retired node")
    definitions: dict[str, Condition] = {}
    validate_conditions(task.goal_conditions, state, predicates, definitions)
    enabled = {skill.name for skill in skills.skills}
    groups_by_purpose = {}
    for node in proposal.subgoals:
        purpose = purpose_key(node.success_conditions)
        if purpose in groups_by_purpose and groups_by_purpose[purpose] != node.recovery_group_id:
            raise ExecutionFault("RECOVERY_BUDGET_RESET", "equivalent subgoals must share a recovery budget")
        groups_by_purpose[purpose] = node.recovery_group_id
        if not set(node.candidate_skills) <= enabled:
            raise ExecutionFault("INVALID_PLAN", f"unknown skill in {node.id}")
        if len(node.depends_on) != len(set(node.depends_on)) or not set(node.depends_on) <= nodes.keys():
            raise ExecutionFault("INVALID_PLAN", f"duplicate/dangling dependency in {node.id}")
        if node.id not in retired and set(node.depends_on) & retired:
            raise ExecutionFault("INVALID_PLAN", "active node depends on a superseded node")
        validate_conditions(node.preconditions + node.success_conditions, state, predicates, definitions)
    visiting, visited = set(), set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ExecutionFault("INVALID_PLAN", "dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in nodes[node_id].depends_on:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)
    goals = {goal.id: goal for goal in task.goal_conditions}
    if set(proposal.goal_coverage) != set(goals):
        raise ExecutionFault("GOAL_NOT_COVERED", "coverage must name every frozen final goal exactly")
    for goal_id, providers in proposal.goal_coverage.items():
        if not providers or len(providers) != len(set(providers)):
            raise ExecutionFault("GOAL_NOT_COVERED", goal_id)
        for provider in providers:
            if provider not in nodes or provider in retired or goals[goal_id] not in nodes[provider].success_conditions:
                raise ExecutionFault("GOAL_NOT_COVERED", f"{provider} does not establish {goal_id}")
