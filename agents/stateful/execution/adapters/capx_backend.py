"""Reuse a constructed Cap-X environment's actual API registry.

This adapter never calls env.step/_exec_user_code: generated namespaces must
not acquire env, APIS or persistent globals. The owner supplies state and
stop/status hooks appropriate to the chosen simulation backend. Only trusted
fixed scripts are supported in this in-process foundation.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from ...contracts import BackendProfile, MotionState, StateView, utc_now
from ..backend import CallContext, CameraFrame, ExecutionFault, Observation


class CapXBackend:
    def __init__(
        self, *, functions: Mapping[str, Callable], observe: Callable[[], Observation],
        motion_state: Callable[[], MotionState], stop: Callable[[], MotionState],
        validators: Mapping[str, Callable], backend_id: str, catalog_version: str,
        session_id: str | None = None,
    ):
        if set(validators) != set(functions):
            raise ValueError("every enabled API needs an explicit argument/operating-limit validator")
        self._functions = dict(functions)
        self._validators = dict(validators)
        self._observe, self._motion_state, self._stop = observe, motion_state, stop
        self.catalog_version = catalog_version
        self.profile = BackendProfile(
            backend_id=backend_id, session_id=session_id or str(uuid.uuid4()), mode="simulation",
            cooperative_deadline=False, cooperative_cancel=False, stop_confirmation=True,
        )

    @classmethod
    def from_env(
        cls, env: Any, *, state_provider: Callable[[], StateView],
        motion_state: Callable[[], MotionState], stop: Callable[[], MotionState],
        validators: Mapping[str, Callable], backend_id: str, catalog_version: str,
        session_id: str | None = None,
    ) -> CapXBackend:
        def observe() -> Observation:
            state = state_provider()
            frame = env.render()
            frames = []
            if frame is not None:
                from PIL import Image

                observed_at = utc_now()
                buffer = io.BytesIO()
                Image.fromarray(frame).save(buffer, format="PNG")
                frames.append(CameraFrame("main", buffer.getvalue(), "png", observed_at))
            return Observation(state=state, frames=frames)

        return cls(functions=env.api_functions(), observe=observe, motion_state=motion_state,
                   stop=stop, validators=validators, backend_id=backend_id,
                   catalog_version=catalog_version, session_id=session_id)

    def functions(self) -> Mapping[str, Callable]:
        return self._functions.copy()

    def invoke(self, name: str, args: tuple, kwargs: dict, context: CallContext) -> Any:
        context.checkpoint()
        try:
            self._validators[name](*args, **kwargs)
        except (ValueError, TypeError) as exc:
            raise ExecutionFault("ARGUMENT_INVALID", str(exc)) from exc
        result = self._functions[name](*args, **kwargs)
        # A legacy blocking API cannot be preempted here. Capabilities say so.
        context.checkpoint()
        return result

    def observe(self) -> Observation:
        return self._observe()

    def motion_state(self) -> MotionState:
        return self._checked_state(self._motion_state())

    def stop(self) -> MotionState:
        return self._checked_state(self._stop())

    @staticmethod
    def _checked_state(value: str) -> MotionState:
        if value not in {"idle", "running", "unknown"}:
            raise ValueError("backend hook must return idle, running or unknown")
        return value
