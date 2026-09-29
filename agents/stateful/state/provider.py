"""A local reference P adapter; no independent progress database or model votes."""

from typing import Protocol

from ..contracts import Fact, StateView, canonical_json, digest
from ..execution.backend import ExecutionFault
from ..feedback.service import key, timestamp
from .contracts import PStateAck, PUpdateRequest


def state_signature(state):
    if state is None:
        return None
    return digest(canonical_json({"episode": state.episode_id, "objects": sorted(state.objects),
                 "facts": sorted([(f.predicate, f.args, f.value, f.source) for f in state.facts], key=str)}))


class PAdapter(Protocol):
    name: str

    def update(self, request: PUpdateRequest, previous: PStateAck | None, *, timeout_s: float) -> PStateAck | None:
        """Apply idempotently by update_id; None means pending. Remote adapters must honor timeout_s."""
        ...


def merge_state(request, previous):
    raw = request.observation
    if raw and raw.episode_id != request.episode_id:
        raise ExecutionFault("P_EPISODE_MISMATCH")
    if request.feedback and (request.feedback.episode_id != request.episode_id or
                            request.feedback.feedback_id != request.feedback_id):
        raise ExecutionFault("P_FEEDBACK_MISMATCH")
    history_only = bool(previous and request.event_watermark < previous.event_watermark)
    if previous and raw and timestamp(raw.observed_at) < timestamp(previous.state.observed_at):
        history_only = True
    if history_only:
        return PStateAck(update_id=request.update_id, episode_id=request.episode_id,
                         snapshot_id=f"{request.update_id}.state", event_watermark=previous.event_watermark,
                         processed_execution_id=request.execution_id, processed_report_revision=request.report_revision,
                         processed_feedback_id=request.feedback_id, source_state_version=previous.source_state_version,
                         state=previous.state, conflicts=previous.conflicts, history_only=True)
    objects = raw.objects if raw else previous.state.objects if previous else []
    candidates = {}
    if previous:
        candidates = {key(f): [] for f in previous.state.facts}
    if raw:
        for fact in raw.facts:
            accepted = (fact.source == request.source and bool(fact.evidence_refs)
                        and timestamp(fact.observed_at) <= timestamp(raw.observed_at))
            candidates.setdefault(key(fact), []).append((fact.value if accepted else None,
                                 fact.observed_at, fact.evidence_refs if accepted else []))
    if request.feedback:
        for claim in request.feedback.terminal_claims:
            if not set(claim.args) <= set(objects):
                continue
            roots = sorted({root for ref in claim.evidence_refs for root in request.evidence_lineage.get(ref, [ref])})
            candidates.setdefault(key(claim), []).append((claim.value, claim.observed_at, roots))
    facts, conflicts = [], []
    for (predicate, args), values in sorted(candidates.items()):
        known = {value for value, _, _ in values if value is not None}
        value = next(iter(known)) if len(known) == 1 else None
        if len(known) > 1:
            conflicts.append(f"{predicate}({','.join(args)})")
        # Missing observations invalidate current certainty; old facts remain in historical snapshots.
        observed_at = max((stamp for _, stamp, _ in values), key=timestamp, default=request.observed_at)
        facts.append(Fact(predicate=predicate, args=list(args), value=value, observed_at=observed_at,
                          source=request.source, evidence_refs=sorted({r for _, _, refs in values for r in refs})))
    state = StateView(episode_id=request.episode_id, state_version=raw.state_version if raw else 0,
                      observed_at=raw.observed_at if raw else request.observed_at,
                      objects=objects, facts=facts, evidence_refs=sorted({r for f in facts for r in f.evidence_refs}))
    version = max(state.state_version, previous.state.state_version +
                  int(state_signature(previous.state) != state_signature(state)) if previous else 0)
    state = state.model_copy(update={"state_version": version})
    return PStateAck(update_id=request.update_id, episode_id=request.episode_id,
                     snapshot_id=f"{request.update_id}.state", event_watermark=request.event_watermark,
                     processed_execution_id=request.execution_id, processed_report_revision=request.report_revision,
                     processed_feedback_id=request.feedback_id,
                     source_state_version=raw.state_version if raw else None, state=state, conflicts=conflicts)


class LocalPAdapter:
    name = "local-evidence-fusion-v1"

    def update(self, request, previous, *, timeout_s):
        return merge_state(request, previous)


def validate_ack(ack, request, previous):
    expected = merge_state(request, previous)
    for field in ("update_id", "episode_id", "snapshot_id", "event_watermark", "processed_execution_id",
                  "processed_report_revision", "processed_feedback_id", "source_state_version", "history_only"):
        if getattr(ack, field) != getattr(expected, field):
            raise ExecutionFault("P_ACK_MISMATCH", f"P did not acknowledge the requested input: {field}")
    if (ack.state.episode_id != expected.state.episode_id or ack.state.state_version < expected.state.state_version
            or ack.state.objects != expected.state.objects or ack.state.observed_at != expected.state.observed_at):
        raise ExecutionFault("P_STATE_MISMATCH")
    actual = {key(f): f for f in ack.state.facts}
    expected_facts = {key(f): f for f in expected.state.facts}
    if actual.keys() != expected_facts.keys() or not set(expected.conflicts) <= set(ack.conflicts):
        raise ExecutionFault("P_STATE_EVIDENCE_MISMATCH")
    for fact_key, fact in actual.items():
        supported = expected_facts[fact_key]
        if (fact.value is not None and fact.value != supported.value or fact.source != supported.source
                or fact.observed_at != supported.observed_at or fact.evidence_refs != supported.evidence_refs):
            raise ExecutionFault("P_STATE_EVIDENCE_MISMATCH")
