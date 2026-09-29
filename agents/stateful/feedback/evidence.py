"""Read only captured, hashed evidence from the current execution window."""

import hashlib
from datetime import datetime

from ..contracts import ExecutionIdentity, FrameManifest, StateView, canonical_json, digest
from ..execution.backend import ExecutionFault
from .contracts import AnalysisQuery, CoFRequest, EvidenceRecord


def read_evidence(store, path, *, limit, sha256=None):
    target = (store.directory / path).resolve()
    if not target.is_relative_to(store.directory) or not target.is_file() or target.stat().st_size > limit:
        raise ExecutionFault("COF_EVIDENCE_PATH_OR_SIZE")
    data = target.read_bytes()
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise ExecutionFault("COF_EVIDENCE_HASH_MISMATCH")
    return data


def bounded(records, count):
    if len(records) <= count:
        return records
    # Preserve the beginning/end and sample intervening call boundaries evenly.
    return [records[round(index * (len(records) - 1) / (count - 1))] for index in range(count)]


def build_request(manager, report, limits, extra_queries=()):
    store = manager.store
    entry = store.lookup(report.execution_id)
    if not entry or entry["report"] != report:
        raise ExecutionFault("COF_REPORT_MISMATCH")
    current = manager.read()
    node = next(n for n in current.plan.subgoals if n.id == report.subgoal_id)
    segment = current.segments[report.execution_id]
    definitions = {c.id: c for c in node.success_conditions + segment.expected_conditions +
                   segment.continue_conditions + manager.task.goal_conditions}
    queries = [AnalysisQuery(query_id=f"query-{i}", condition=c) for i, c in enumerate(definitions.values(), 1)]
    queries.extend(extra_queries)
    records, gaps, used_bytes = [], [], 0
    events = store.events(report.execution_id)
    report_events = [e for e in events if e["kind"] == "ExecutionReported" and
                     e["payload"]["report_revision"] == report.report_revision]
    watermark = report_events[-1]["seq"]
    start = datetime.fromisoformat(report.started_at)
    cutoff = datetime.fromisoformat(report_events[-1]["timestamp"])

    def check_window(stamp, boundary):
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if (observed.tzinfo is None or observed.utcoffset().total_seconds() != 0 or observed > cutoff
                or boundary != "before_execution" and observed < start):
            raise ExecutionFault("COF_EVIDENCE_TIME_MISMATCH")
    if report.frame_manifest_ref:
        raw = read_evidence(store, report.frame_manifest_ref, limit=1048576)
        manifest = FrameManifest.model_validate_json(raw)
        if manifest.execution_id != report.execution_id:
            raise ExecutionFault("COF_MANIFEST_MISMATCH")
        if report.frame_manifest_hash and hashlib.sha256(canonical_json(manifest.model_dump(mode="json")).encode()).hexdigest() != report.frame_manifest_hash:
            raise ExecutionFault("COF_MANIFEST_HASH_MISMATCH")
        gaps.extend(manifest.gaps)
        selected = bounded(manifest.frames, limits.max_frames) if report.frame_manifest_hash else []
        if not report.frame_manifest_hash:
            gaps.append("unhashed historical frame manifest excluded")
        if len(selected) < len(manifest.frames):
            gaps.append("frame selection omitted some call boundaries")
        for frame in selected:
            if frame.execution_id != report.execution_id:
                raise ExecutionFault("COF_FRAME_EXECUTION_MISMATCH")
            if not frame.sha256:
                gaps.append(f"unhashed historical frame excluded: {frame.frame_id}")
                continue
            try:
                check_window(frame.observed_at, frame.boundary)
                data = read_evidence(store, frame.path, limit=limits.max_evidence_bytes, sha256=frame.sha256)
                if used_bytes + len(data) > limits.max_evidence_bytes:
                    gaps.append("evidence byte budget omitted a frame")
                    continue
                used_bytes += len(data)
                records.append(EvidenceRecord(evidence_id=f"{report.execution_id}.{frame.frame_id}",
                               kind="frame", path=frame.path, sha256=frame.sha256,
                               observed_at=frame.observed_at, boundary=frame.boundary, source=frame.source,
                               call_id=frame.call_id, camera_id=frame.camera_id))
            except (OSError, ValueError, ExecutionFault) as exc:
                gaps.append(str(exc))
    captured = [e for e in events if e["kind"] == "ObservationCaptured" and e["seq"] <= watermark
                and e["payload"].get("state_ref")]
    captured += [{**e, "payload": {**e["payload"], "boundary": "reconcile", "call_id": None}}
                 for e in events if e["kind"] == "BackendStopConfirmed" and e["payload"].get("phase") == "reconcile"
                 and e["seq"] <= watermark and e["payload"].get("state_ref")]
    captured.sort(key=lambda e: e["seq"])
    selected = bounded(captured, limits.max_state_records)
    if len(selected) < len(captured):
        gaps.append("state selection omitted some call boundaries")
    for event in selected:
        data = event["payload"]
        try:
            raw = read_evidence(store, data["state_ref"], limit=limits.max_evidence_bytes)
            state = StateView.model_validate_json(raw)
            check_window(state.observed_at, data["boundary"])
            checksum = hashlib.sha256(canonical_json(state.model_dump(mode="json")).encode()).hexdigest()
            if not data.get("state_hash") or checksum != data["state_hash"]:
                raise ExecutionFault("COF_STATE_HASH_MISMATCH")
            if state.episode_id != report.episode_id:
                raise ExecutionFault("COF_STATE_EPISODE_MISMATCH")
            if used_bytes + len(raw) > limits.max_evidence_bytes:
                gaps.append("evidence byte budget omitted a state record")
                continue
            used_bytes += len(raw)
            records.append(EvidenceRecord(evidence_id=f"{report.execution_id}.state-{event['seq']}",
                           kind="state", path=data["state_ref"], sha256=hashlib.sha256(raw).hexdigest(),
                           observed_at=state.observed_at, boundary=data["boundary"], source=manager.source,
                           call_id=data["call_id"], state=state))
        except (OSError, ValueError, ExecutionFault) as exc:
            gaps.append(str(exc))
    records.sort(key=lambda e: (e.observed_at, e.evidence_id))
    # No API arguments, stdout, RESULT or success labels are supplied as effect evidence.
    objects = sorted({arg for e in records if e.state for arg in e.state.objects})
    if not objects:
        objects = sorted({arg for c in definitions.values() for arg in c.args})
    payload = dict(**{k: getattr(report, k) for k in ExecutionIdentity.model_fields},
                   report_revision=report.report_revision, report_hash=digest(report.model_dump_json()),
                   event_watermark=watermark, objects=objects,
                   predicates={p.name: p.arity for p in manager.predicates.predicates},
                   queries=[q.model_dump(mode="json") for q in queries],
                   evidence=[e.model_dump(mode="json") for e in records], gaps=gaps)
    input_hash = digest(canonical_json(payload))
    return CoFRequest(request_id=f"cof-{report.execution_id}-r{report.report_revision}-{input_hash[-12:]}",
                      input_hash=input_hash, **payload)
