"""Length-framed strict-worker broker protocol."""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hermes_cli.kanban_store.canonical import canonical_json_bytes
from hermes_cli.kanban_store.types import ContractError, RunFence, RuntimeIdentity

from .capabilities import BROKER_METHODS, CapabilityManifest

MAX_FRAME_BYTES = 1_048_576
_REQUEST_FIELDS = {
    "schema", "request_id", "seq", "task_id", "run_id", "claim_generation",
    "method", "params",
}


class ProtocolError(RuntimeError):
    pass



_PARAM_CONTRACTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "inference.request": (
        frozenset({"profile", "messages"}),
        frozenset({"profile", "messages", "max_tokens", "reasoning_effort", "temperature"}),
    ),
    "event.append": (
        frozenset({"event_uuid", "event_type", "severity", "retention_class", "payload"}),
        frozenset({
            "event_uuid", "event_type", "severity", "retention_class", "payload",
            "correlation_id", "operation_id", "stream", "stream_seq", "producer_time",
        }),
    ),
    "intent.draft": (
        frozenset({"kind", "target", "payload", "client_nonce"}),
        frozenset({"kind", "target", "payload", "client_nonce"}),
    ),
    "artifact.declare": (
        frozenset({"relative_path", "display_name", "media_type"}),
        frozenset({"relative_path", "display_name", "media_type"}),
    ),
    "heartbeat": (frozenset({"ttl_seconds"}), frozenset({"ttl_seconds"})),
    "finalize": (
        frozenset({"outcome", "summary"}),
        frozenset({"outcome", "summary", "metadata"}),
    ),
}

def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ProtocolError("unexpected EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ProtocolError("frame size outside contract")
    payload = recv_exact(sock, size)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError("malformed JSON frame") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame must be an object")
    return value


def send_frame(sock: socket.socket, value: Mapping[str, object]) -> None:
    payload = canonical_json_bytes(dict(value))
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("response frame exceeds limit")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


@dataclass(slots=True)
class SessionState:
    fence: RunFence
    runtime_identity: RuntimeIdentity
    last_seq: int = 0
    finalized: bool = False


FenceValidator = Callable[[RunFence], None]
Handler = Callable[[Mapping[str, Any], RunFence], Mapping[str, Any]]


class BrokerSession:
    def __init__(
        self,
        *,
        state: SessionState,
        manifest: CapabilityManifest,
        fence_validator: FenceValidator,
        handlers: Mapping[str, Handler],
    ) -> None:
        self.state = state
        self.manifest = manifest
        self.fence_validator = fence_validator
        self.handlers = dict(handlers)

    def _validate_params(self, method: str, params: Mapping[str, Any]) -> None:
        required, allowed = _PARAM_CONTRACTS[method]
        fields = set(params)
        if not required <= fields or not fields <= allowed:
            raise ProtocolError(f"{method} params do not match the V1 schema")
        max_params = int(self.manifest.limits.get("max_param_bytes", MAX_FRAME_BYTES))
        if len(canonical_json_bytes(dict(params))) > max_params:
            raise ProtocolError("broker params exceed the capability limit")
        if method == "inference.request":
            if params["profile"] not in self.manifest.inference_profiles:
                raise PermissionError("inference profile is not granted")
            if not isinstance(params["messages"], list):
                raise ProtocolError("inference messages must be a list")
            if "max_tokens" in params:
                maximum = int(self.manifest.limits.get("max_tokens", 0))
                value = params["max_tokens"]
                if not isinstance(value, int) or value < 1 or (maximum and value > maximum):
                    raise ProtocolError("max_tokens exceeds the capability limit")
        elif method == "event.append":
            if not isinstance(params["payload"], dict):
                raise ProtocolError("event payload must be an object")
        elif method == "intent.draft":
            if not isinstance(params["target"], dict) or not isinstance(params["payload"], dict):
                raise ProtocolError("intent target and payload must be objects")
        elif method == "heartbeat":
            ttl = params["ttl_seconds"]
            if not isinstance(ttl, int) or ttl < 30 or ttl > 86400:
                raise ProtocolError("heartbeat ttl is outside the contract")
        elif method == "finalize":
            if params["outcome"] not in {"completed", "blocked", "review", "changes"}:
                raise ProtocolError("unsupported finalization outcome")
            if not isinstance(params["summary"], str):
                raise ProtocolError("finalization summary must be a string")
            if "metadata" in params and not isinstance(params["metadata"], dict):
                raise ProtocolError("finalization metadata must be an object")

    def _validate(self, request: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        if set(request) != _REQUEST_FIELDS:
            raise ProtocolError("unknown or missing request fields")
        if request["schema"] != "hermes.kanban.broker.v1":
            raise ProtocolError("unsupported broker schema")
        request_id = request["request_id"]
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ProtocolError("invalid request_id")
        seq = request["seq"]
        if not isinstance(seq, int) or seq != self.state.last_seq + 1:
            raise ProtocolError("out-of-order or duplicate sequence")
        if (
            request["task_id"] != self.state.fence.task_id
            or int(request["run_id"]) != self.state.fence.run_id
            or int(request["claim_generation"]) != self.state.fence.claim_generation
        ):
            raise ProtocolError("cross-run or cross-generation request")
        method = request["method"]
        if not isinstance(method, str) or method not in BROKER_METHODS:
            raise ProtocolError("unsupported broker method")
        if method not in self.manifest.broker_methods:
            raise PermissionError(f"broker method denied: {method}")
        params = request["params"]
        if not isinstance(params, dict):
            raise ProtocolError("params must be an object")
        if self.state.finalized:
            raise ProtocolError("session already finalized")
        self._validate_params(method, params)
        self.fence_validator(self.state.fence)
        self.state.last_seq = seq
        return method, params

    def handle(self, request: Mapping[str, Any]) -> dict[str, object]:
        request_id = str(request.get("request_id", ""))
        try:
            method, params = self._validate(request)
            handler = self.handlers.get(method)
            if handler is None:
                raise ProtocolError("broker handler unavailable")
            trusted_params = dict(params)
            if method == "finalize":
                trusted_params["_trusted_runtime_identity"] = (
                    self.state.runtime_identity.as_dict()
                )
            result = dict(handler(trusted_params, self.state.fence))
            if method == "finalize":
                self.state.finalized = True
            return {
                "schema": "hermes.kanban.broker-response.v1",
                "request_id": request_id,
                "seq": self.state.last_seq,
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            return {
                "schema": "hermes.kanban.broker-response.v1",
                "request_id": request_id,
                "seq": self.state.last_seq,
                "ok": False,
                "error": {"code": type(exc).__name__, "message": str(exc)[:512]},
            }

    def serve(self, sock: socket.socket) -> None:
        while True:
            request = recv_frame(sock)
            response = self.handle(request)
            send_frame(sock, response)
            if self.state.finalized or not response["ok"]:
                return
