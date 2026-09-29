"""Deterministic lifting fixture, not a physics simulator or perception model."""

from __future__ import annotations

import math
import uuid

from ...contracts import BackendProfile, Fact, StateView, utc_now
from ..backend import CallContext, CameraFrame, ExecutionFault, Observation

SCENARIOS = (
    "normal", "missed_grasp", "partial_error", "timed_out", "stop_unknown", "cancelled",
    "observation_missing",
)


class FakeBackend:
    catalog_version = "fake_franka_lift_v1"

    def __init__(self, episode_id: str, scenario: str = "normal"):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario: {scenario}")
        self.episode_id, self.scenario = episode_id, scenario
        self.profile = BackendProfile(
            backend_id="fake-franka", session_id=str(uuid.uuid4()), mode="mock",
            cooperative_deadline=True, cooperative_cancel=True, stop_confirmation=True,
        )
        self.version = 0
        self.holding, self.lifted = False, False
        self.gripper_open = True
        self.calls: list[str] = []
        self._motion = "idle"
        self.stop_available = scenario != "stop_unknown"

    def functions(self) -> dict:
        return {"close_gripper": self.close_gripper, "open_gripper": self.open_gripper,
                "move_to_joints": self.move_to_joints}

    def close_gripper(self) -> None:
        """Fixture: close the gripper; a normal return does not prove a grasp."""
        self.gripper_open = False
        self.holding = self.scenario != "missed_grasp"

    def open_gripper(self) -> None:
        """Fixture: release the object back onto the table."""
        self.gripper_open = True
        self.holding, self.lifted = False, False

    def move_to_joints(self, joints: list[float]) -> None:
        """Fixture: any valid seven-joint target represents the lift segment (radians)."""
        if not isinstance(joints, list) or len(joints) != 7 or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or abs(value) > math.pi for value in joints
        ):
            raise ExecutionFault("ARGUMENT_INVALID", "expected seven finite joint angles within [-pi, pi]")
        self.lifted = self.holding

    def invoke(self, name: str, args: tuple, kwargs: dict, context: CallContext):
        context.checkpoint()
        self.calls.append(name)
        self._motion = "running"
        try:
            result = self.functions()[name](*args, **kwargs)
            self.version += 1
            if name == "move_to_joints":
                if self.scenario == "partial_error":
                    raise ExecutionFault("API_ERROR", "injected failure after the lift changed the scene")
                if self.scenario in {"timed_out", "stop_unknown"}:
                    raise ExecutionFault("DEADLINE_EXCEEDED", "injected backend deadline")
                if self.scenario == "cancelled":
                    context.cancellation.set()
            context.checkpoint()
            return result
        finally:
            if self.stop_available or name != "move_to_joints":
                self._motion = "idle"

    def motion_state(self):
        return self._motion

    def stop(self):
        if self.stop_available:
            self._motion = "idle"
        return self._motion if self.stop_available else "unknown"

    def observe(self) -> Observation:
        if self.scenario == "observation_missing" and self.calls:
            raise RuntimeError("injected camera/state-provider outage")
        timestamp = utc_now()
        source_ref = f"mock-sensor-v{self.version}"
        facts = [
            Fact(predicate=predicate, args=args, value=value, observed_at=timestamp,
                 source="mock", evidence_refs=[source_ref])
            for predicate, args, value in (
                ("holding", ["robot", "red_cube"], self.holding),
                ("lifted", ["red_cube"], self.lifted),
                ("gripper_open", ["robot"], self.gripper_open),
            )
        ]
        state = StateView(episode_id=self.episode_id, state_version=self.version, observed_at=timestamp,
                          objects=["robot", "red_cube"], facts=facts, evidence_refs=[source_ref])
        # Tiny lossless PPMs keep the fixture dependency-free and visibly synthetic.
        pixels = bytearray()
        cube_y = 12 if self.lifted else 32
        for y in range(48):
            for x in range(48):
                color = (215, 50, 50) if 18 <= x < 30 and cube_y <= y < cube_y + 10 else (
                    (90, 100, 110) if y >= 42 else (235, 239, 243))
                pixels.extend(color)
        frame = CameraFrame("fixture-camera", b"P6\n48 48\n255\n" + pixels, "ppm", timestamp)
        return Observation(state=state, frames=[frame])
