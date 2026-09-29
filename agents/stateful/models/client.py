"""Explicit chat-completions transport with finite retries and durable accounting.

No dependency on the legacy LLM client's implicit routing or unbounded retry loop.
The injectable transport makes the entire protocol testable without a network.
"""

from __future__ import annotations

import json
import base64
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..contracts import canonical_json
from ..execution.backend import ExecutionFault
from ..execution.store import EventStore
from .contracts import ModelConfig, ModelLimits
from .http_worker import NoRedirect, request_http

PROMPT_VERSION = "stateful-models-v1"


def strict_json(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"nonfinite JSON constant: {value}")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    # Also rejects overflows such as 1e999, which json.loads otherwise accepts.
    canonical_json(value)
    return value


class ModelFault(ExecutionFault):
    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.retryable = retryable


@dataclass(frozen=True)
class HTTPReply:
    status: int
    body: bytes


class HTTPTransport:
    def __init__(self, opener=None):
        # Injected opener is for in-process unit tests; production always uses a
        # killable HTTP process so a slow DNS/header read cannot defeat timeout.
        self.opener = opener

    def __call__(self, endpoint, headers, body, timeout_s, max_response_bytes):
        request = dict(endpoint=endpoint, headers=headers, body=body,
                       timeout_s=timeout_s, max_response_bytes=max_response_bytes)
        if self.opener is not None:
            result = request_http(**request, opener=self.opener)
        else:
            request["body"] = base64.b64encode(body).decode()
            # Preserve the user's proxy/CA choices without passing unrelated secrets.
            environment = {key: value for key, value in os.environ.items()
                           if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
                           or key in {"SSL_CERT_FILE", "SSL_CERT_DIR"}}
            environment.update(PATH=os.defpath, LC_ALL="C")
            process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).with_name("http_worker.py"))],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=environment)
            try:
                output, _ = process.communicate(canonical_json(request).encode(), timeout=timeout_s)
                if process.returncode or len(output) > 2 * max_response_bytes + 4096:
                    raise ModelFault("MODEL_HTTP_WORKER_ERROR")
                result = strict_json(output.decode())
            except subprocess.TimeoutExpired:
                raise ModelFault("MODEL_TIMEOUT", retryable=True) from None
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        if "error" in result:
            raise ModelFault(result["error"], retryable=result["retryable"])
        return HTTPReply(result["status"], base64.b64decode(result["body"]))


class ModelClient:
    def __init__(self, config: ModelConfig, store: EventStore, limits: ModelLimits | None = None,
                 *, transport: Callable | None = None, setting: str = "live_model"):
        self.config, self.store = config, store
        self.limits = limits or ModelLimits()
        self.transport = transport or HTTPTransport()
        self.setting = setting
        self._secret = None
        store.bind_extension("model", {"config": config.model_dump(mode="json"),
                                       "limits": self.limits.model_dump(mode="json"),
                                       "setting": setting, "prompt_version": PROMPT_VERSION})

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self._secret, "[REDACTED]") if self._secret else value
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value

    def usage(self):
        requested = [e["payload"] for e in self.store.events() if e["kind"] == "ModelCallRequested"]
        usage = [e["payload"]["usage"] for e in self.store.events() if e["kind"] == "ModelUsageReported"]
        return {"model_calls": len(requested),
                "reserved_output_tokens": sum(e["reserved_output_tokens"] for e in requested),
                "reported_prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
                "reported_completion_tokens": sum(u.get("completion_tokens", 0) for u in usage),
                "calls_without_completion_usage": len(requested) - sum("completion_tokens" in u for u in usage)}

    def complete(self, messages: list[dict], *, purpose: str, repair_index: int = 0) -> str:
        if self.config.api_key_env:
            self._secret = os.environ.get(self.config.api_key_env)
            if not self._secret:
                raise ModelFault("MODEL_KEY_ENV_MISSING")
            if "\n" in self._secret or "\r" in self._secret:
                raise ModelFault("MODEL_KEY_ENV_INVALID")
        body = {"model": self.config.model, "messages": messages,
                self.config.token_parameter: self.config.max_output_tokens, "stream": False}
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if self.config.json_object_mode:
            body["response_format"] = {"type": "json_object"}
        encoded = canonical_json(body).encode()
        if len(encoded) > self.limits.max_context_bytes:
            raise ModelFault("MODEL_CONTEXT_TOO_LARGE")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        for attempt in range(self.config.max_http_attempts):
            used = self.usage()
            if (used["model_calls"] >= self.limits.max_calls or
                    used["reserved_output_tokens"] + self.config.max_output_tokens > self.limits.max_reserved_output_tokens):
                raise ModelFault("MODEL_BUDGET_EXHAUSTED")
            call_id = f"model-{used['model_calls'] + 1}"
            request = {"call_id": call_id, "purpose": purpose, "repair_index": repair_index,
                       "http_attempt": attempt + 1, "prompt_version": PROMPT_VERSION,
                       "request": self.redact(body), "reserved_output_tokens": self.config.max_output_tokens}
            # A crash here still consumes the reservation; unknown HTTP attempts are not refunded.
            self.store.append("ModelCallRequested", call_id, request)
            self.store.write_json(f"models/{call_id}/request.json", request)
            start = time.monotonic()
            try:
                reply = self.transport(self.config.endpoint, headers, encoded, self.config.timeout_s,
                                       self.limits.max_response_bytes)
                if not 200 <= reply.status < 300:
                    raise ModelFault(f"MODEL_HTTP_{reply.status}",
                                     retryable=reply.status in {408, 429, 500, 502, 503, 504})
                if len(reply.body) > self.limits.max_response_bytes:
                    raise ModelFault("MODEL_RESPONSE_TOO_LARGE")
                raw = self.redact(reply.body.decode("utf-8"))
                self.store.write_json(f"models/{call_id}/response.json", {"raw": raw})
                # Keep raw replies in the authoritative ledger as well as in convenient exports.
                self.store.append("ModelResponseReceived", call_id, {"raw": raw})
                response = strict_json(raw)
                raw_usage = response.get("usage") or {}
                if not isinstance(raw_usage, dict):
                    raise ModelFault("MODEL_RESPONSE_INVALID")
                usage = {key: value for key, value in raw_usage.items()
                         if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                         and type(value) is int and value >= 0}
                self.store.append("ModelUsageReported", call_id, {"usage": usage})
                choices = response.get("choices")
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ModelFault("MODEL_RESPONSE_INVALID")
                choice = choices[0]
                message = choice.get("message", {})
                if choice.get("finish_reason") != "stop":
                    raise ModelFault("MODEL_RESPONSE_INCOMPLETE")
                if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
                    raise ModelFault("MODEL_RESPONSE_UNSUPPORTED")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ModelFault("MODEL_RESPONSE_EMPTY")
                completed = {"call_id": call_id, "usage": usage, "latency_s": time.monotonic() - start}
                self.store.append("ModelCallCompleted", call_id, completed)
                self.store.write_json(f"models/{call_id}/result.json", completed)
                return content
            except Exception as exc:
                fault = exc if isinstance(exc, ModelFault) else ModelFault("MODEL_RESPONSE_INVALID")
                payload = {"call_id": call_id, "reason": fault.reason,
                           "latency_s": time.monotonic() - start}
                self.store.append("ModelCallFailed", call_id, payload)
                self.store.write_json(f"models/{call_id}/error.json", payload)
                if not fault.retryable or attempt + 1 == self.config.max_http_attempts:
                    raise fault from None
        raise AssertionError("unreachable")
