from typing import Annotated, Literal

from pydantic import Field

from ..contracts import Contract, Identifier, StateView
from ..feedback.contracts import CoFFeedback


class StateLimits(Contract):
    max_update_attempts: Annotated[int, Field(ge=1, le=5)] = 3
    update_timeout_s: Annotated[float, Field(gt=0, le=60)] = 10.0
    max_updates: Annotated[int, Field(ge=1, le=1000)] = 200


class PUpdateRequest(Contract):
    update_id: Identifier
    episode_id: Identifier
    previous_snapshot_id: Identifier | None
    event_watermark: int
    execution_id: Identifier | None
    report_revision: int | None
    feedback_id: Identifier | None
    observation: StateView | None
    feedback: CoFFeedback | None
    evidence_lineage: dict[str, list[str]] = Field(default_factory=dict)
    source: Literal["mock", "observed"]
    observed_at: str


class PStateAck(Contract):
    update_id: Identifier
    episode_id: Identifier
    snapshot_id: Identifier
    event_watermark: int
    processed_execution_id: Identifier | None
    processed_report_revision: int | None
    processed_feedback_id: Identifier | None
    source_state_version: int | None
    state: StateView
    conflicts: list[str]
    history_only: bool = False
