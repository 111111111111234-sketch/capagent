"""Deterministic adapter for explicit P/fake facts, not visual or CoF inference."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..contracts import Condition, StateView, canonical_json, digest, utc_now
from ..execution.backend import ExecutionFault
from ..execution.validation import check_time
from .contracts import ConditionCheck, VerificationReport, VerificationRequest


def state_hash(state: StateView) -> str:
    return digest(canonical_json(state.model_dump(mode="json")))


def condition_checks(conditions: list[Condition], state: StateView, *, max_age: float,
                     source: str, not_before: str | None = None) -> list[ConditionCheck]:
    facts = {(fact.predicate, tuple(fact.args)): fact for fact in state.facts}
    checks = []
    for condition in conditions:
        fact = facts.get((condition.predicate, tuple(condition.args)))
        verdict = "unknown"
        if fact is not None and fact.value is not None and fact.source == source and fact.evidence_refs:
            try:
                check_time(state.observed_at, max_age)
                check_time(fact.observed_at, max_age)
                fresh = not_before is None or (
                    datetime.fromisoformat(fact.observed_at.replace("Z", "+00:00"))
                    >= datetime.fromisoformat(not_before.replace("Z", "+00:00")))
                if fresh:
                    verdict = "pass" if fact.value == condition.expected else "fail"
            except (ExecutionFault, ValueError, TypeError):
                pass
        checks.append(ConditionCheck(condition=condition, verdict=verdict,
                                     source=fact.source if fact else "missing",
                                     observed_at=fact.observed_at if fact else None,
                                     evidence_refs=fact.evidence_refs if fact else []))
    return checks


def aggregate(checks: list[ConditionCheck]) -> str:
    verdicts = {check.verdict for check in checks}
    # Any missing evidence keeps a subgoal pending even when another condition fails.
    return "unknown" if "unknown" in verdicts else "fail" if "fail" in verdicts else "pass"


def verify(request: VerificationRequest, state: StateView, *, max_age: float,
           source: str, report_id: str | None = None) -> VerificationReport:
    if state.episode_id != request.episode_id or state.state_version < request.min_state_version:
        raise ExecutionFault("VERIFICATION_STATE_MISMATCH")
    return VerificationReport(
        report_id=report_id or f"verify-{uuid.uuid4()}", request_id=request.request_id,
        episode_id=request.episode_id, plan_version=request.plan_version, scope=request.scope,
        subject_id=request.subject_id, execution_id=request.execution_id,
        state_version=state.state_version, state_hash=state_hash(state), checked_at=utc_now(),
        groups={name: condition_checks(conditions, state, max_age=max_age, source=source,
                                      not_before=request.not_before)
                for name, conditions in request.groups.items()},
    )
