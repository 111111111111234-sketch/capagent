"""Execute one validated segment, via a worker or trusted fixture, and report facts."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import threading
import time
from typing import Any

from ..contracts import (
    ExecutionIdentity, ExecutionReport, ExecutionRequest, FrameManifest, FrameRef, StateView,
    TaskSpec, canonical_json, utc_now,
)
from .backend import Backend, CallContext, ExecutionFault, Observation
from .store import EventStore
from .validation import build_catalog, validate_request, validate_state


class LimitedOutput(io.StringIO):
    def __init__(self, limit: int = 16384):
        super().__init__()
        self.remaining = limit
        self.truncated = False

    def write(self, value: str) -> int:
        data = value.encode()
        self.truncated |= len(data) > self.remaining
        kept = data[:self.remaining].decode(errors="ignore")
        self.remaining -= len(kept.encode())
        super().write(kept)
        return len(value)


def summarize(value: Any) -> dict:
    """Do not serialize large sensor arrays or arbitrary backend objects into events."""
    if value is None or isinstance(value, (str, bool, int, float, list, tuple, dict)):
        try:
            encoded = canonical_json(value)
            if len(encoded.encode()) <= 2048:
                return {"value": json.loads(encoded)}
        except (TypeError, ValueError):
            pass
    return {"type": type(value).__name__, "summary_omitted": True}


class Executor:
    def __init__(self, task: TaskSpec, backend: Backend, store: EventStore, *, execution_mode: str = "trusted_in_process"):
        # Snapshot nested containers as well as the frozen top-level contracts.
        self.task = TaskSpec.model_validate_json(task.model_dump_json())
        self.backend = backend
        self.store = store
        self.functions = dict(backend.functions())
        self.catalog = build_catalog(backend.catalog_version, self.functions)
        if execution_mode not in {"trusted_in_process", "process"}:
            raise ValueError("unsupported execution mode")
        self.execution_mode = execution_mode
        self.preflight_state_provider = None
        store.bind(self.task, backend.profile, self.catalog)
        store.bind_extension("execution_worker", {"mode": execution_mode})
        self._cancellation = threading.Event()
        self._active: str | None = None
        self._run_lock = threading.Lock()

    def cancel(self, execution_id: str) -> bool:
        """Signal a running call; its backend must cooperate to interrupt mid-call."""
        if self._active != execution_id:
            return False
        self._cancellation.set()
        return True

    def execute(self, request: ExecutionRequest) -> ExecutionReport:
        if not self._run_lock.acquire(blocking=False):
            raise ExecutionFault("BACKEND_BUSY")
        try:
            return self._execute(ExecutionRequest.model_validate_json(request.model_dump_json()))
        finally:
            self._active = None
            self._run_lock.release()

    def _execute(self, request: ExecutionRequest) -> ExecutionReport:
        old = self.store.lookup(request.execution_id)
        if old:
            if old["request_hash"] != request.request_hash:
                raise ExecutionFault("ID_CONFLICT", "execution ID already belongs to a different request")
            return old["report"] or self._unknown_after_restart(old)
        try:
            validate_request(request, self.task, self.catalog)
            if self.store.pending():
                raise ExecutionFault("EXECUTION_UNKNOWN", "an earlier execution still needs reconciliation")
            if self.backend.motion_state() != "idle":
                raise ExecutionFault("BACKEND_BUSY", "backend has not confirmed idle")
            before = self.backend.observe()
            state = self.preflight_state_provider(before) if self.preflight_state_provider else before.state
            validate_state(request, state)
        except Exception as exc:
            self.store.append("ExecutionRejected", request.execution_id,
                              {"request_hash": request.request_hash, "error": str(exc),
                               "reason": exc.reason if isinstance(exc, ExecutionFault) else "PREFLIGHT_ERROR"})
            raise
        started = self.store.register(request, self.task)
        self._cancellation.clear()
        self._active = request.execution_id
        run = _Run(self, request, started)
        return run.execute(before)

    def _unknown_after_restart(self, entry: dict) -> ExecutionReport:
        request = entry["request"]
        events = self.store.events(request.execution_id)
        return ExecutionReport(
            **identity(request), report_id=f"{request.execution_id}.r1",
            catalog_version=request.catalog_version, state_version_before=request.based_on_state_version,
            code_hash=request.code_hash, request_hash=request.request_hash,
            status="outcome_unknown", runtime_rc=None, started_at=entry["started_at"], ended_at=None,
            execution_mode=self.execution_mode,
            backend_motion_state="unknown", stop_reason="RESTART_REQUIRES_RECONCILIATION",
            partial_effects_possible=True, api_calls=sum(e["kind"] == "CallRequested" for e in events),
            wall_time_s=None, error="dispatch has no durable final report; it must not be replayed",
        )

    def reconcile(self, execution_id: str) -> ExecutionReport:
        """Stop/query the original backend session and observe; never re-run code."""
        if not self._run_lock.acquire(blocking=False):
            raise ExecutionFault("BACKEND_BUSY")
        try:
            entry = self.store.lookup(execution_id)
            if entry is None:
                raise ExecutionFault("UNKNOWN_EXECUTION_ID")
            old = entry["report"] or self._unknown_after_restart(entry)
            if old.status != "outcome_unknown":
                return old
            self.store.append("ReconciliationRequested", execution_id, {})
            try:
                motion = self.backend.stop()
            except Exception:
                motion = "unknown"
            evidence = list(old.evidence_refs)
            missing = list(old.feedback_missing)
            state_ref = None
            if motion == "idle":
                try:
                    observation = self.backend.observe()
                    if observation.state is None or observation.state.episode_id != self.task.episode_id:
                        raise ValueError("state provider returned no matching state")
                    state_ref = self.store.write_json(
                        f"executions/{execution_id}/reconcile-state-{old.report_revision + 1}.json",
                        observation.state.model_dump(mode="json"),
                    )
                    evidence.append(state_ref)
                except Exception as exc:
                    missing.append(f"reconcile observation: {type(exc).__name__}: {exc}")
            stop_event = self.store.append("BackendStopConfirmed" if motion == "idle" else "BackendStopUnknown",
                                           execution_id, {"motion_state": motion, "phase": "reconcile",
                                                          "state_ref": state_ref,
                                                          "state_hash": hashlib.sha256(canonical_json(observation.state.model_dump(mode="json")).encode()).hexdigest()
                                                          if state_ref else None})
            revision = old.report_revision + 1
            # Confirmed stop does not reconstruct the lost Python result or prove success.
            data = old.model_dump(mode="json")
            data.update(report_id=f"{execution_id}.r{revision}", report_revision=revision,
                        status="error" if motion == "idle" else "outcome_unknown",
                        backend_motion_state=motion, ended_at=utc_now() if motion == "idle" else None,
                        stop_reason="RECONCILED_STOP" if motion == "idle" else "STOP_UNCONFIRMED",
                        stop_evidence_refs=[f"event:{stop_event}"],
                        state_after_ref=state_ref, evidence_refs=evidence, feedback_missing=missing)
            report = ExecutionReport.model_validate(data)
            self.store.save_report(report)
            return report
        finally:
            self._run_lock.release()


def identity(request: ExecutionRequest) -> dict:
    return {key: getattr(request, key) for key in ExecutionIdentity.model_fields}


class _Run:
    def __init__(self, executor: Executor, request: ExecutionRequest, started_at: str):
        self.executor, self.request, self.started_at = executor, request, started_at
        self.store, self.backend = executor.store, executor.backend
        self.started = time.monotonic()
        self.deadline = self.started + request.budget.max_wall_time_s
        self.manifest = FrameManifest(execution_id=request.execution_id)
        self.api_calls = 0
        self.partial = False
        self.fault: ExecutionFault | None = None
        self.evidence: list[str] = []
        self.state_ref: str | None = None
        self.capture_count = 0
        self.base = f"executions/{request.execution_id}"

    def context(self, call_id: str = "segment") -> CallContext:
        return CallContext(self.request.execution_id, call_id, self.deadline, self.executor._cancellation)

    def capture(self, boundary: str, call_id: str | None = None,
                observation: Observation | None = None) -> None:
        self.capture_count += 1
        self.state_ref = None
        try:
            observation = observation or self.backend.observe()
            if observation.state and observation.state.episode_id != self.request.episode_id:
                raise ValueError("observation belongs to another episode")
        except Exception as exc:
            # Sensor failures preserve runtime facts. Storage failures below must
            # propagate instead of letting an unlogged action proceed.
            self.manifest.gaps.append(f"{boundary}: {type(exc).__name__}: {exc}")
            return
        if observation.state is not None:
            self.state_ref = self.store.write_json(
                f"{self.base}/state-{self.capture_count}.json", observation.state.model_dump(mode="json"))
            self.evidence.append(self.state_ref)
        else:
            self.manifest.gaps.append(f"{boundary}: state unavailable")
        if not observation.frames:
            self.manifest.gaps.append(f"{boundary}: no camera frame")
        frame_ids = []
        for frame in observation.frames:
            if len(self.manifest.frames) >= self.request.budget.max_frames:
                self.manifest.gaps.append(f"{boundary}: frame budget exhausted")
                break
            if frame.extension not in {"png", "ppm", "jpg"} or len(frame.data) > 16 * 1024 * 1024:
                self.manifest.gaps.append(f"{boundary}: unsupported or oversized frame")
                continue
            frame_id = f"frame-{len(self.manifest.frames) + 1}"
            reference = FrameRef(
                frame_id=frame_id, execution_id=self.request.execution_id, call_id=call_id,
                camera_id=frame.camera_id, observed_at=frame.observed_at,
                source="mock" if self.backend.profile.mode == "mock" else "observed",
                boundary=boundary, path=f"{self.base}/{frame_id}.{frame.extension}",
                sha256=hashlib.sha256(frame.data).hexdigest(),
            )
            self.store.write_bytes(reference.path, frame.data)
            self.manifest.frames.append(reference)
            frame_ids.append(frame_id)
            self.evidence.append(reference.path)
        self.store.append("ObservationCaptured", self.request.execution_id,
                          {"boundary": boundary, "call_id": call_id, "state_ref": self.state_ref,
                           "frame_ids": frame_ids,
                           "state_hash": hashlib.sha256(canonical_json(observation.state.model_dump(mode="json")).encode()).hexdigest()
                           if observation.state else None})

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.fault:
            raise self.fault
        self.api_calls += 1
        call_id = f"call-{self.api_calls}"
        event = {"call_id": call_id, "call_seq": self.api_calls, "api": name,
                 "arguments": summarize({"args": args, "kwargs": kwargs})}
        self.store.append("CallRequested", self.request.execution_id, event)
        began = False
        try:
            context = self.context(call_id)
            context.checkpoint()
            if self.api_calls > self.request.budget.max_api_calls:
                raise ExecutionFault("BUDGET_EXCEEDED", "segment API budget exhausted")
            if self.store.usage()["api_calls"] > self.executor.task.budget.max_api_calls:
                raise ExecutionFault("BUDGET_EXCEEDED", "episode API budget exhausted")
            try:
                inspect.signature(self.executor.functions[name]).bind(*args, **kwargs)
            except TypeError as exc:
                raise ExecutionFault("ARGUMENT_INVALID", str(exc)) from exc
            self.store.append("CallStarted", self.request.execution_id, event)
            began = True
            self.partial = True
            result = self.backend.invoke(name, args, kwargs, context)
            context.checkpoint()
            if self.backend.motion_state() != "idle":
                raise ExecutionFault("EXECUTION_UNKNOWN", "API returned without confirmed backend idle")
            self.store.append("CallReturned", self.request.execution_id,
                              {**event, "result": summarize(result), "motion_state": "idle"})
            return result
        except Exception as exc:
            self.fault = exc if isinstance(exc, ExecutionFault) else ExecutionFault("API_ERROR", str(exc))
            self.store.append("CallFailed" if began else "CallRejected", self.request.execution_id,
                              {**event, "reason": self.fault.reason, "error": str(exc)})
            if self.fault is exc:
                raise
            raise self.fault from exc
        finally:
            if began:
                self.capture("after_call", call_id)

    def execute(self, before: Observation) -> ExecutionReport:
        self.store.write_bytes(f"{self.base}/code.py", self.request.code.encode())
        self.store.write_json(f"{self.base}/request.json", self.request.model_dump(mode="json"))
        self.capture("before_execution", observation=before)
        stdout, stderr = LimitedOutput(), LimitedOutput()
        namespace = {"__builtins__": {}, "INPUTS": json.loads(canonical_json(self.request.inputs)),
                     "RESULT": None, "print": print}
        for name in self.request.allowed_skills:
            namespace[name] = lambda *a, _name=name, **kw: self.call(_name, *a, **kw)
        runtime_rc = None
        result_ref = None
        error = None
        reason = "NORMAL_RETURN"
        try:
            self.context().checkpoint()
            if self.executor.execution_mode == "process":
                from .worker import run_worker

                worker = run_worker(self.request.code, self.request.inputs, self.request.allowed_skills,
                                    call=self.call, checkpoint=self.context().checkpoint,
                                    timeout_s=self.request.budget.max_wall_time_s)
                stdout.write(worker["stdout"])
                stdout.truncated |= worker["truncated"]
                if worker["failure"]:
                    raise worker["failure"]
                if worker["error"]:
                    raise ExecutionFault("WORKER_ERROR", worker["error"])
                namespace["RESULT"] = worker["result"]
            else:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exec(compile(self.request.code, "<fixed-script>", "exec"), namespace, namespace)
            runtime_rc = 0
            self.context().checkpoint()
            encoded = canonical_json(namespace["RESULT"]).encode()
            if len(encoded) > 65536:
                raise ExecutionFault("RESULT_TOO_LARGE")
            result_ref = self.store.write_bytes(f"{self.base}/result.json", encoded)
        except (Exception, KeyboardInterrupt) as exc:
            runtime_rc = 1 if runtime_rc is None else runtime_rc
            reason = exc.reason if isinstance(exc, ExecutionFault) else (
                "CANCEL_REQUESTED" if isinstance(exc, KeyboardInterrupt) else "WORKER_ERROR")
            error = f"{type(exc).__name__}: {exc}"
            stderr.write(error + "\n")
        try:
            motion = self.backend.stop() if error else self.backend.motion_state()
        except Exception as exc:
            motion = "unknown"
            error = error or f"stop/status query failed: {exc}"
        if motion != "idle":
            status = "outcome_unknown"
        elif reason == "DEADLINE_EXCEEDED":
            status = "timed_out"
        elif reason == "CANCEL_REQUESTED":
            status = "cancelled"
        else:
            status = "error" if error else "completed"
        stop_event = self.store.append("BackendStopConfirmed" if motion == "idle" else "BackendStopUnknown",
                                       self.request.execution_id, {"motion_state": motion, "reason": reason})
        self.capture("after_execution")
        manifest_ref = self.store.write_json(f"{self.base}/frames.json", self.manifest.model_dump(mode="json"))
        report = ExecutionReport(
            **identity(self.request), report_id=f"{self.request.execution_id}.r1",
            catalog_version=self.request.catalog_version,
            state_version_before=self.request.based_on_state_version,
            code_hash=self.request.code_hash, request_hash=self.request.request_hash,
            status=status, runtime_rc=runtime_rc, started_at=self.started_at,
            execution_mode=self.executor.execution_mode,
            ended_at=utc_now() if motion == "idle" else None, backend_motion_state=motion,
            stop_evidence_refs=[f"event:{stop_event}"],
            stop_reason=reason, partial_effects_possible=self.partial, api_calls=self.api_calls,
            wall_time_s=time.monotonic() - self.started, evidence_refs=self.evidence,
            frame_manifest_ref=manifest_ref, state_after_ref=self.state_ref, result_ref=result_ref,
            frame_manifest_hash=hashlib.sha256(canonical_json(self.manifest.model_dump(mode="json")).encode()).hexdigest(),
            stdout_ref=self.store.write_bytes(f"{self.base}/stdout.txt", stdout.getvalue().encode()),
            stderr_ref=self.store.write_bytes(f"{self.base}/stderr.txt", stderr.getvalue().encode()),
            output_truncated=stdout.truncated or stderr.truncated,
            feedback_missing=self.manifest.gaps, error=error,
        )
        self.store.save_report(report)
        return report
