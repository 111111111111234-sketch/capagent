"""Model-authored proposals exclude runtime identities, budgets and outcomes."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..contracts import Condition, Contract, Identifier, Positive


class ModelConfig(Contract):
    model: Annotated[str, Field(min_length=1, max_length=256)]
    endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = None
    timeout_s: Annotated[float, Field(gt=0, le=60)] = 30.0
    max_http_attempts: Annotated[int, Field(ge=1, le=3)] = 2
    max_output_tokens: Annotated[int, Field(ge=128, le=16384)] = 2048
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    json_object_mode: bool = False

    @model_validator(mode="after")
    def explicit_endpoint(self):
        url = urlsplit(self.endpoint)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path or any(c.isspace() for c in self.endpoint)):
            raise ValueError("endpoint must be a full HTTP(S) completion URL without credentials/query/fragment")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote model endpoints require HTTPS")
        if not self.model.strip():
            raise ValueError("model must be explicit")
        _ = url.port
        return self


class ModelLimits(Contract):
    max_calls: Annotated[int, Field(ge=1, le=200)] = 32
    max_reserved_output_tokens: Annotated[int, Field(ge=128, le=1000000)] = 65536
    max_repairs: Annotated[int, Field(ge=0, le=5)] = 2
    max_context_bytes: Annotated[int, Field(ge=4096, le=524288)] = 131072
    max_response_bytes: Annotated[int, Field(ge=4096, le=1048576)] = 131072
    max_loop_steps: Annotated[int, Field(ge=1, le=500)] = 100


class CodeProposal(Contract):
    based_on_state_version: Annotated[int, Field(ge=0)]
    plan_version: Positive
    subgoal_id: Identifier
    intent: Annotated[str, Field(min_length=1, max_length=2000)]
    code: Annotated[str, Field(min_length=1, max_length=16000)]
    entry_conditions: list[Condition]
    expected_conditions: list[Condition] = Field(min_length=1)
    continue_conditions: list[Condition] = Field(default_factory=list)
