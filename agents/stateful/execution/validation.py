"""A small straight-line Python subset shared by trusted scripts and model workers.

This validation is not an OS sandbox. Process mode independently interprets the
same restricted language without granting Python object or builtins access.
"""

from __future__ import annotations

import ast
import inspect
import math
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from ..contracts import ExecutionRequest, SkillCatalog, SkillSpec, StateView, TaskSpec
from .backend import ExecutionFault


def build_catalog(version: str, functions: Mapping[str, Callable]) -> SkillCatalog:
    for name in functions:
        if not name.isidentifier() or name.startswith("_") or name in {"INPUTS", "RESULT", "print"}:
            raise ValueError(f"reserved or invalid API name: {name}")
    return SkillCatalog(catalog_version=version, skills=[
        SkillSpec(name=name, signature=str(inspect.signature(fn)), description=inspect.getdoc(fn) or "")
        for name, fn in sorted(functions.items())
    ])


def validate_code(code: str, allowed: set[str]) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ExecutionFault("CODE_INVALID", str(exc)) from exc
    if not tree.body or sum(1 for _ in ast.walk(tree)) > 4096:
        raise ExecutionFault("CODE_INVALID", "empty or oversized syntax tree")
    permitted = (
        ast.Module, ast.Expr, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant,
        ast.Call, ast.keyword, ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.UnaryOp, ast.USub,
    )
    names = {"INPUTS", "RESULT"} | allowed | {"print"}
    for statement in tree.body:
        for node in ast.walk(statement):
            if not isinstance(node, permitted):
                raise ExecutionFault("CODE_INVALID", f"unsupported Python construct: {type(node).__name__}")
            if isinstance(node, ast.Constant) and (type(node.value) not in {str, int, float, bool, type(None)}
                                                  or isinstance(node.value, float) and not math.isfinite(node.value)):
                raise ExecutionFault("CODE_INVALID", "only finite JSON literals are supported")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in allowed | {"print"}:
                    raise ExecutionFault("API_NOT_ALLOWED", "calls must name an enabled API directly")
                if any(kw.arg is None for kw in node.keywords):
                    raise ExecutionFault("CODE_INVALID", "keyword expansion is unsupported")
                if node.func.id == "print" and any(kw.arg not in {"sep", "end"} for kw in node.keywords):
                    raise ExecutionFault("CODE_INVALID", "print only supports sep and end")
            if isinstance(node, ast.Name):
                if node.id.startswith("_"):
                    raise ExecutionFault("CODE_INVALID", "private names are unsupported")
                if isinstance(node.ctx, ast.Load) and node.id not in names:
                    raise ExecutionFault("CODE_INVALID", f"undefined name: {node.id}")
            if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
                raise ExecutionFault("CODE_INVALID", "dictionary expansion is unsupported")
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if not isinstance(target, ast.Name) or target.id in allowed | {"INPUTS", "print"}:
                    raise ExecutionFault("CODE_INVALID", "assignment must target a local variable or RESULT")
                names.add(target.id)


def validate_request(request: ExecutionRequest, task: TaskSpec, catalog: SkillCatalog) -> None:
    if (request.episode_id, request.task_version) != (task.episode_id, task.task_version):
        raise ExecutionFault("TASK_MISMATCH")
    if request.catalog_version != catalog.catalog_version:
        raise ExecutionFault("CATALOG_MISMATCH")
    allowed = set(request.allowed_skills)
    if not allowed <= {skill.name for skill in catalog.skills}:
        raise ExecutionFault("API_NOT_ALLOWED")
    validate_code(request.code, allowed)


def validate_state(request: ExecutionRequest, state: StateView | None) -> None:
    if state is None:
        raise ExecutionFault("STATE_UNAVAILABLE")
    if (state.episode_id, state.state_version) != (request.episode_id, request.based_on_state_version):
        raise ExecutionFault("STATE_STALE")
    check_time(state.observed_at, request.max_state_age_s)
    facts = {(fact.predicate, tuple(fact.args)): fact for fact in state.facts}
    for condition in request.entry_conditions:
        fact = facts.get((condition.predicate, tuple(condition.args)))
        if fact is None or fact.value is None or fact.source == "predicted" or not fact.evidence_refs:
            raise ExecutionFault("PRECONDITION_UNKNOWN", condition.id)
        check_time(fact.observed_at, request.max_state_age_s)
        if fact.value != condition.expected:
            raise ExecutionFault("PRECONDITION_FAILED", condition.id)


def check_time(value: str, max_age: float) -> None:
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("timestamp must have a timezone")
        age = (datetime.now(timezone.utc) - observed).total_seconds()
    except ValueError as exc:
        raise ExecutionFault("STATE_STALE", str(exc)) from exc
    if age < -1 or age > max_age:
        raise ExecutionFault("STATE_STALE", "observation timestamp is not current")
