"""Validate references, derive temporal evidence, and persist bounded CoF reports."""

import time
from datetime import datetime

from ..contracts import ExecutionIdentity
from ..execution.backend import ExecutionFault
from .contracts import CoFFeedback, CoFProposal, ConditionEvidence, FeedbackLimits, TimelineEvent
from .evidence import build_request


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ExecutionFault("COF_CLOCK_MISMATCH")
    return parsed


def key(claim):
    return claim.predicate, tuple(claim.args)


def validate_proposal(proposal, request):
    known = {e.evidence_id: e for e in request.evidence}
    ids = set()
    for claim in proposal.claims:
        if claim.claim_id in ids:
            raise ExecutionFault("COF_DUPLICATE_CLAIM")
        ids.add(claim.claim_id)
        if request.predicates.get(claim.predicate) != len(claim.args) or not set(claim.args) <= set(request.objects):
            raise ExecutionFault("COF_UNKNOWN_PREDICATE_OR_OBJECT")
        observed = timestamp(claim.observed_at)
        for ref in claim.evidence_refs:
            record = known.get(ref)
            if record is None:
                raise ExecutionFault("COF_UNKNOWN_EVIDENCE_REFERENCE")
            if record.kind == "frame" and observed != timestamp(record.observed_at):
                raise ExecutionFault("COF_CLAIM_TIME_MISMATCH")
            if record.state is not None:
                facts = [f for f in record.state.facts if key(f) == key(claim)]
                if (not facts or facts[0].source != record.source or not facts[0].evidence_refs
                        or facts[0].value != claim.value or facts[0].observed_at != claim.observed_at
                        or observed > timestamp(record.observed_at)):
                    raise ExecutionFault("COF_SENSOR_CLAIM_MISMATCH")


def terminal_claims(proposal, request):
    records = {e.evidence_id: e for e in request.evidence}
    latest = {}
    for e in request.evidence:
        if e.boundary in {"after_execution", "reconcile"}:
            channel = (e.kind, e.camera_id)
            latest[channel] = max(latest.get(channel, timestamp(e.observed_at)), timestamp(e.observed_at))
    return [c for c in proposal.claims if any(records[r].boundary in {"after_execution", "reconcile"}
            and timestamp(c.observed_at) == latest.get((records[r].kind, records[r].camera_id)) for r in c.evidence_refs)]


def assess(request, proposal):
    terminal = terminal_claims(proposal, request)
    checks, events = [], []
    for query in request.queries:
        candidates = [c for c in (terminal if query.scope == "at_end" else proposal.claims)
                      if key(c) == key(query.condition)]
        values = {c.value for c in candidates}
        verdict, reason = "unknown", "missing, conflicting or insufficient evidence"
        if query.scope == "at_end" and len(values) == 1 and None not in values:
            verdict = "supported" if next(iter(values)) == query.condition.expected else "refuted"
            reason = "latest observed terminal fact"
        elif query.scope == "occurred" and query.condition.expected in values:
            verdict, reason = "supported", "observed at a cited instant, not a continuous interval"
        elif query.scope in {"maintained", "stable_for_window"}:
            reason = "call-boundary samples do not establish continuous/window coverage"
        checks.append(ConditionEvidence(query_id=query.query_id, condition_id=query.condition.id,
                                        assessment=verdict, reason=reason,
                                        evidence_refs=sorted({r for c in candidates for r in c.evidence_refs})))
    grouped = {}
    records = {e.evidence_id: e for e in request.evidence}
    for claim in proposal.claims:
        channels = {(records[r].kind, records[r].camera_id) for r in claim.evidence_refs}
        if len(channels) != 1:
            continue
        grouped.setdefault((key(claim), next(iter(channels))), {}).setdefault(claim.observed_at, []).append(claim)
    for ((predicate, args), _channel), instants in grouped.items():
        previous = None
        for moment, claims in sorted(instants.items(), key=lambda item: timestamp(item[0])):
            values = {c.value for c in claims}
            current = next(iter(values)) if len(values) == 1 else None
            if previous and previous[1] is not None and current is not None and previous[1] != current:
                events.append(TimelineEvent(predicate=predicate, args=list(args), before=previous[1], after=current,
                              started_at=previous[0], ended_at=moment,
                              evidence_refs=sorted(set(previous[2] + [r for c in claims for r in c.evidence_refs])),
                              description="observed relation changed; no physical cause inferred"))
            previous = (moment, current, [r for c in claims for r in c.evidence_refs])
    return terminal, checks, events


class FeedbackService:
    def __init__(self, manager, analyzer, limits=None):
        self.manager, self.store, self.analyzer = manager, manager.store, analyzer
        self.limits = limits or FeedbackLimits()
        self.store.bind_extension("cof", {"analyzer": analyzer.name, "limits": self.limits.model_dump(mode="json")})

    def analyze(self, report, extra_queries=()):
        request = build_request(self.manager, report, self.limits, extra_queries)
        old = [e["payload"] for e in self.store.events() if e["kind"] == "CoFFeedbackProduced"
               and e["payload"]["request_id"] == request.request_id]
        if old:
            return request, CoFFeedback.model_validate(old[-1])
        self.store.append("CoFRequested", report.execution_id, request.model_dump(mode="json"))
        self.store.write_json(f"feedback/{request.request_id}/request.json", request.model_dump(mode="json"))
        start, error, proposal = time.monotonic(), None, None
        for attempt in range(self.limits.max_attempts_per_request):
            if sum(e["kind"] == "CoFAnalysisRequested" for e in self.store.events()) >= self.limits.max_analyses:
                error = "COF_ANALYSIS_BUDGET_EXHAUSTED"
                break
            self.store.append("CoFAnalysisRequested", report.execution_id,
                              {"request_id": request.request_id, "attempt": attempt + 1})
            try:
                candidate = self.analyzer.analyze(request, error)
                candidate = CoFProposal.model_validate_json(candidate.model_dump_json())
                validate_proposal(candidate, request)
                proposal = candidate
                break
            except Exception as exc:
                error = getattr(exc, "reason", type(exc).__name__)
                self.store.append("CoFAnalysisRejected", report.execution_id,
                                  {"request_id": request.request_id, "error": error, "attempt": attempt + 1})
        proposal = proposal or CoFProposal()
        terminal, checks, events = assess(request, proposal)
        gaps = list(request.gaps)
        if error and not proposal.claims:
            gaps.append(error)
        if not any(e.kind == "frame" and e.boundary == "after_execution" for e in request.evidence):
            gaps.append("terminal camera frame unavailable")
        if any(c.assessment == "unknown" for c in checks):
            gaps.append("some queried conditions lack sufficient evidence")
        feedback = CoFFeedback(**{k: getattr(report, k) for k in ExecutionIdentity.model_fields},
                    feedback_id=f"feedback-{request.request_id}", request_id=request.request_id,
                    report_revision=report.report_revision, input_hash=request.input_hash,
                    event_watermark=request.event_watermark,
                    analysis_status="unavailable" if not proposal.claims else "partial" if gaps else "ready",
                    claims=proposal.claims, terminal_claims=terminal, events=events, condition_evidence=checks,
                    uncertainties=gaps, observation_requests=["refresh current facts for unresolved conditions"]
                    if any(c.assessment == "unknown" for c in checks) else [], analyzer=self.analyzer.name,
                    selected_frames=sum(e.kind == "frame" for e in request.evidence),
                    selected_state_records=sum(e.kind == "state" for e in request.evidence),
                    elapsed_s=time.monotonic() - start)
        self.store.append("CoFFeedbackProduced", report.execution_id, feedback.model_dump(mode="json"))
        self.store.write_json(f"feedback/{request.request_id}/feedback.json", feedback.model_dump(mode="json"))
        return request, feedback
