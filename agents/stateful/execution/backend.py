"""Backend protocol; execution and rendering stay on the calling thread."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import BackendProfile, MotionState, StateView


class ExecutionFault(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass
class CallContext:
    execution_id: str
    call_id: str
    deadline: float
    cancellation: threading.Event

    def checkpoint(self) -> None:
        if self.cancellation.is_set():
            raise ExecutionFault("CANCEL_REQUESTED")
        if time.monotonic() >= self.deadline:
            raise ExecutionFault("DEADLINE_EXCEEDED")


@dataclass(frozen=True)
class CameraFrame:
    camera_id: str
    data: bytes
    extension: str
    observed_at: str


@dataclass
class Observation:
    state: StateView | None
    frames: list[CameraFrame] = field(default_factory=list)


class Backend(Protocol):
    profile: BackendProfile
    catalog_version: str

    def functions(self) -> Mapping[str, Callable[..., Any]]: ...

    def invoke(self, name: str, args: tuple, kwargs: dict, context: CallContext) -> Any: ...

    def observe(self) -> Observation: ...

    def motion_state(self) -> MotionState: ...

    def stop(self) -> MotionState: ...
