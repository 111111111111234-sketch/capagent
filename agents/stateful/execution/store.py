"""Single-writer execution ledger. SQLite is authoritative; JSONL is an export.

The owner lock is held for this run directory's lifetime, including backend
calls. An unfinished dispatch survives process death and blocks new dispatches.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from collections.abc import Callable
from pathlib import Path

from ..contracts import (
    BackendProfile, ExecutionIdentity, ExecutionReport, ExecutionRequest, SkillCatalog, TaskSpec,
    canonical_json, utc_now,
)
from .backend import ExecutionFault


class EventStore:
    def __init__(self, directory: str | Path):
        self.dispatch_guard: Callable[[ExecutionRequest], dict] | None = None
        self.feedback_guard: Callable[[ExecutionRequest], None] | None = None
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = (self.directory / "owner.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise ExecutionFault("BACKEND_BUSY", "run directory already has an owner") from exc
        try:
            self.db = sqlite3.connect(self.directory / "events.sqlite3")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                    request TEXT NOT NULL, started_at TEXT NOT NULL, report TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL, execution_id TEXT, payload TEXT NOT NULL
                );
            """)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lock.close()
            raise

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()
        self._lock.close()

    def bind(self, task: TaskSpec, profile: BackendProfile, catalog: SkillCatalog) -> None:
        metadata = {"task": task.model_dump(mode="json"), "backend": profile.model_dump(mode="json"),
                    "catalog": catalog.model_dump(mode="json")}
        with self.db:
            for key, value in metadata.items():
                serialized = canonical_json(value)
                old = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                if old and old[0] != serialized:
                    raise ExecutionFault("RUN_MISMATCH", f"frozen {key} differs; use a new run directory")
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, serialized))
        for key, value in metadata.items():
            self.write_json(f"{key}.json", value)

    def _event(self, kind: str, execution_id: str | None, payload: dict) -> int:
        cursor = self.db.execute("INSERT INTO events(timestamp,kind,execution_id,payload) VALUES (?,?,?,?)",
                                 (utc_now(), kind, execution_id, canonical_json(payload)))
        return cursor.lastrowid

    def append(self, kind: str, execution_id: str, payload: dict) -> int:
        with self.db:
            return self._event(kind, execution_id, payload)

    def bind_extension(self, key: str, value: dict) -> None:
        """Freeze a cooperating module's configuration in this same ledger."""
        encoded = canonical_json(value)
        with self.db:
            previous = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if previous and previous[0] != encoded:
                raise ExecutionFault("RUN_MISMATCH", f"frozen {key} differs")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, encoded))

    def lookup(self, execution_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT request_hash,request,started_at,report FROM executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        return {"request_hash": row[0], "request": ExecutionRequest.model_validate_json(row[1]),
                "started_at": row[2],
                "report": ExecutionReport.model_validate_json(row[3]) if row[3] else None}

    def pending(self) -> list[str]:
        rows = self.db.execute("SELECT execution_id,report FROM executions").fetchall()
        return [eid for eid, report in rows if report is None or json.loads(report)["status"] == "outcome_unknown"]

    def usage(self) -> dict[str, int]:
        return {
            "executions": self.db.execute("SELECT COUNT(*) FROM executions").fetchone()[0],
            "api_calls": self.db.execute("SELECT COUNT(*) FROM events WHERE kind='CallRequested'").fetchone()[0],
        }

    def register(self, request: ExecutionRequest, task: TaskSpec) -> str:
        with self.db:
            if self.pending():
                raise ExecutionFault("EXECUTION_UNKNOWN", "reconcile the existing dispatch before another action")
            if self.usage()["executions"] >= task.budget.max_executions:
                raise ExecutionFault("BUDGET_EXCEEDED", "episode execution budget exhausted")
            planning_enabled = self.db.execute("SELECT 1 FROM metadata WHERE key='planning'").fetchone()
            if planning_enabled and self.dispatch_guard is None:
                raise ExecutionFault("PLANNING_GUARD_REQUIRED", "restore the planning manager before dispatch")
            if self.db.execute("SELECT 1 FROM metadata WHERE key='closed_loop'").fetchone():
                if self.feedback_guard is None:
                    raise ExecutionFault("FEEDBACK_GUARD_REQUIRED")
                self.feedback_guard(request)
            segment = self.dispatch_guard(request) if self.dispatch_guard else None
            started = utc_now()
            self.db.execute("INSERT INTO executions VALUES (?,?,?,?,NULL)",
                            (request.execution_id, request.request_hash, request.model_dump_json(), started))
            self._event("ExecutionDispatched", request.execution_id,
                        {"request_hash": request.request_hash, "request": request.model_dump(mode="json"),
                         "planning_segment": segment})
        return started

    def save_report(self, report: ExecutionReport) -> None:
        with self.db:
            entry = self.lookup(report.execution_id)
            if entry is None or entry["request_hash"] != report.request_hash:
                raise ExecutionFault("REPORT_MISMATCH")
            request = entry["request"]
            if any(getattr(report, key) != getattr(request, key) for key in ExecutionIdentity.model_fields):
                raise ExecutionFault("REPORT_MISMATCH", "report identities do not match the dispatch")
            if (report.code_hash != request.code_hash or report.catalog_version != request.catalog_version
                    or report.state_version_before != request.based_on_state_version):
                raise ExecutionFault("REPORT_MISMATCH", "report source does not match the dispatch")
            previous = entry["report"]
            if previous:
                if previous == report:
                    return
                if report.report_revision <= previous.report_revision:
                    raise ExecutionFault("STALE_REPORT")
                if previous.status != "outcome_unknown":
                    raise ExecutionFault("REPORT_ALREADY_FINAL")
            self.db.execute("UPDATE executions SET report=? WHERE execution_id=?",
                            (report.model_dump_json(), report.execution_id))
            self._event("ExecutionReported", report.execution_id, report.model_dump(mode="json"))
        self.write_json(f"executions/{report.execution_id}/report.json", report.model_dump(mode="json"))

    def events(self, execution_id: str | None = None) -> list[dict]:
        query = "SELECT seq,timestamp,kind,execution_id,payload FROM events"
        args = ()
        if execution_id is not None:
            query += " WHERE execution_id=?"
            args = (execution_id,)
        return [{"seq": seq, "timestamp": timestamp, "kind": kind, "execution_id": eid,
                 "payload": json.loads(payload)}
                for seq, timestamp, kind, eid, payload in self.db.execute(query + " ORDER BY seq", args)]

    def write_bytes(self, relative: str, data: bytes) -> str:
        destination = self.directory / relative
        if not destination.resolve().is_relative_to(self.directory):
            raise ValueError("artifact must stay inside the run directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".artifact-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return relative

    def write_json(self, relative: str, value: object) -> str:
        return self.write_bytes(relative, (canonical_json(value) + "\n").encode())

    def export(self) -> None:
        data = "".join(canonical_json(event) + "\n" for event in self.events())
        self.write_bytes("events.jsonl", data.encode())


def replay(directory: str | Path) -> dict:
    """Read existing records without importing or constructing an environment."""
    path = Path(directory).resolve() / "events.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        reports = []
        for eid, raw in db.execute("SELECT execution_id,report FROM executions ORDER BY rowid"):
            reports.append(json.loads(raw) if raw else {"execution_id": eid, "status": "outcome_unknown",
                                                       "stop_reason": "RESTART_REQUIRES_RECONCILIATION"})
        result = {"run_directory": str(path.parent), "reports": reports,
                  "event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                  "api_calls": db.execute("SELECT COUNT(*) FROM events WHERE kind='CallRequested'").fetchone()[0]}
        model_summary = db.execute("SELECT payload FROM events WHERE kind='ModelRunFinished' ORDER BY seq DESC LIMIT 1").fetchone()
        if model_summary:
            result["model_summary"] = json.loads(model_summary[0])
        p_snapshot = db.execute("SELECT payload FROM events WHERE kind='PUpdateApplied' ORDER BY seq DESC").fetchall()
        for (raw,) in p_snapshot:
            ack = json.loads(raw)["ack"]
            if not ack["history_only"]:
                result["p_state"] = ack
                break
        feedback = db.execute("SELECT payload FROM events WHERE kind='CoFFeedbackProduced' ORDER BY seq").fetchall()
        if feedback:
            result["cof_feedback"] = [json.loads(raw) for (raw,) in feedback]
        if db.execute("SELECT 1 FROM events WHERE kind='PlanCreated' LIMIT 1").fetchone():
            from ..planning.progress import restore_events

            events = [{"seq": seq, "timestamp": stamp, "kind": kind, "execution_id": eid,
                       "payload": json.loads(payload)} for seq, stamp, kind, eid, payload in db.execute(
                           "SELECT seq,timestamp,kind,execution_id,payload FROM events ORDER BY seq")]
            planning = restore_events(events)
            result.update(plan=planning.plan.model_dump(mode="json"), progress=planning.progress.model_dump(mode="json"))
        return result
