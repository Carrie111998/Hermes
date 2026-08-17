"""Fail-closed Relay proposal boundary for Hermes tool execution."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    from agent import relay_runtime
except ImportError:  # Relay is optional in older Hermes installations.
    relay_runtime = None

MAX_METADATA_BYTES = 2048
MAX_CANDIDATE_BYTES = 64 * 1024
CLAIM_TTL_SECONDS = 30.0
MAX_CLAIMS = 1024

_TOOL_FIELDS: dict[str, dict[str, type]] = {
    "read_file": {"path": str, "start_line": int, "count": int},
    "write_file": {"path": str, "content": str},
    "patch": {"path": str, "patch": str},
    "terminal": {"command": str, "workdir": str, "timeout": int},
    "web_search": {"query": str, "max_results": int, "recency_days": int},
}
_PATH_FIELDS = frozenset({"path", "workdir"})
_REDACTED_VALUE_FIELDS = frozenset(
    {"content", "patch", "command", "query", "path", "workdir"}
)


class RelayBlockedError(RuntimeError):
    """A bounded, non-secret reason for refusing Relay or dispatch."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if isinstance(reason, str) and reason else "relay_blocked"
        super().__init__(self.reason)


@dataclass(frozen=True)
class EgressDecision:
    allow: bool
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelayPreparation:
    args: dict[str, Any]
    trace: dict[str, Any] | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class _Claim:
    key: tuple[str, str]
    expires_at: float


_CLAIM_LOCK = threading.RLock()
_CLAIMS: dict[tuple[str, str], float] = {}


def _primitive_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return None


def _matches_type(value: Any, expected: type) -> bool:
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _path_class(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "none"
    raw = value.strip()
    lowered = raw.replace("\\", "/").lower()
    if lowered.endswith("/.env") or lowered == ".env" or "/.ssh/" in lowered:
        return "credential"
    if lowered.startswith(("/etc/", "/proc/", "/sys/", "/dev/", "/root/")):
        return "system"
    if os.path.isabs(raw):
        return "absolute"
    return "relative"


def _operation_kind(tool_name: str) -> str:
    if tool_name in {"write_file", "patch"}:
        return "file_mutation"
    if tool_name == "terminal":
        return "command_execution"
    if tool_name == "web_search":
        return "web_query"
    return "file_read"


def _metadata_payload(tool_name: str, args: dict[str, Any]) -> EgressDecision:
    schema = _TOOL_FIELDS.get(tool_name)
    if schema is None:
        return EgressDecision(False, "tool_not_allowlisted")
    if not isinstance(args, dict):
        return EgressDecision(False, "arguments_not_object")
    if set(args) - set(schema):
        return EgressDecision(False, "unknown_argument_field")

    argument_types: dict[str, str] = {}
    redacted: dict[str, bool] = {}
    path_class = "none"
    for field_name, value in args.items():
        if not _matches_type(value, schema[field_name]):
            return EgressDecision(False, "argument_type_mismatch")
        primitive = _primitive_type(value)
        if primitive is None:
            return EgressDecision(False, "non_primitive_argument")
        argument_types[field_name] = primitive
        if field_name in _REDACTED_VALUE_FIELDS:
            redacted[field_name] = True
        if field_name in _PATH_FIELDS:
            current_class = _path_class(value)
            if current_class in {"credential", "system"}:
                return EgressDecision(False, "sensitive_path")
            if path_class == "none":
                path_class = current_class

    payload = {
        "tool_name": tool_name,
        "operation_kind": _operation_kind(tool_name),
        "argument_types": argument_types,
        "size_bytes": len(json.dumps(args, ensure_ascii=False, default=str).encode("utf-8")),
        "item_count": len(args),
        "path_class": path_class,
        "redacted": redacted,
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return EgressDecision(False, "metadata_serialization_failed")
    if len(encoded) > MAX_METADATA_BYTES:
        return EgressDecision(False, "metadata_oversized")
    return EgressDecision(True, "allowed", payload)


def pre_relay_egress(
    tool_name: str,
    args: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    enabled: bool = False,
) -> EgressDecision:
    """Return a metadata-only, fail-closed Relay egress decision."""
    del context
    if not enabled:
        return EgressDecision(True, "disabled_passthrough")
    return _metadata_payload(tool_name, args)


def _cleanup_claims(now: float) -> None:
    for key, expiry in list(_CLAIMS.items()):
        if expiry <= now:
            _CLAIMS.pop(key, None)
    if len(_CLAIMS) > MAX_CLAIMS:
        overflow = len(_CLAIMS) - MAX_CLAIMS
        for key, _ in sorted(_CLAIMS.items(), key=lambda item: item[1])[:overflow]:
            _CLAIMS.pop(key, None)


def claim_execution(session_id: str, task_id: str, tool_call_id: str) -> _Claim:
    """Claim one original tool-call identity immediately before dispatch."""
    key = (session_id or task_id or "", tool_call_id or "")
    if not key[0] or not key[1]:
        raise RelayBlockedError("missing_execution_identity")
    now = time.monotonic()
    with _CLAIM_LOCK:
        _cleanup_claims(now)
        if key in _CLAIMS:
            raise RelayBlockedError("duplicate_execution_claim")
        _CLAIMS[key] = now + CLAIM_TTL_SECONDS
    return _Claim(key, now + CLAIM_TTL_SECONDS)


def release_execution(claim: _Claim | None) -> bool:
    if claim is None:
        return False
    with _CLAIM_LOCK:
        return _CLAIMS.pop(claim.key, None) is not None


def _validate_candidate(tool_name: str, original: dict[str, Any], candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise RelayBlockedError("candidate_not_object")
    if len(json.dumps(candidate, ensure_ascii=False, default=str).encode("utf-8")) > MAX_CANDIDATE_BYTES:
        raise RelayBlockedError("candidate_oversized")
    if "tool_name" in candidate or "args" in candidate:
        if set(candidate) - {"tool_name", "args"}:
            raise RelayBlockedError("candidate_unknown_field")
        if candidate.get("tool_name", tool_name) != tool_name:
            raise RelayBlockedError("tool_name_changed")
        candidate = candidate.get("args")
        if not isinstance(candidate, dict):
            raise RelayBlockedError("candidate_args_not_object")
    schema = _TOOL_FIELDS.get(tool_name)
    if schema is None or set(candidate) - set(schema):
        raise RelayBlockedError("candidate_unknown_field")
    for field_name, value in candidate.items():
        if not _matches_type(value, schema[field_name]):
            raise RelayBlockedError("candidate_type_changed")
    for required in (set(original) & {"path", "command", "query"}):
        if required not in candidate:
            raise RelayBlockedError("candidate_missing_required_field")
    return dict(candidate)


def _run_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RelayBlockedError("relay_event_loop_conflict")


def _runtime_candidate(
    tool_name: str,
    args: dict[str, Any],
    *,
    session_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if relay_runtime is None:
        raise RelayBlockedError("relay_unavailable")
    try:
        runtime, session, parent = relay_runtime.resolve_execution_context(session_id)
        if runtime is None or session is None or not runtime.managed_execution_enabled():
            raise RelayBlockedError("relay_unavailable")
        observed: dict[str, Any] = {}
        callback_context = contextvars.copy_context()

        def capture(next_args: Any) -> Any:
            if not isinstance(next_args, dict):
                raise RelayBlockedError("candidate_not_object")
            observed.update(next_args)
            return next_args

        def guarded(next_args: dict[str, Any]) -> Any:
            with relay_runtime.managed_callback_guard():
                return capture(next_args)

        managed = _run_awaitable(
            runtime.run_in_session_async(
                session,
                runtime.relay.tools.execute,
                tool_name,
                metadata,
                lambda next_args: callback_context.copy().run(guarded, next_args),
                handle=parent,
                metadata=metadata,
            )
        )
        return _validate_candidate(tool_name, args, observed or managed)
    except RelayBlockedError:
        raise
    except Exception as exc:
        raise RelayBlockedError("relay_failed") from exc


def prepare(
    tool_name: str,
    args: dict[str, Any],
    *,
    session_id: str,
    task_id: str,
    tool_call_id: str,
    enabled: bool = False,
) -> RelayPreparation:
    """Perform egress + candidate proposal, without executing a tool."""
    decision = pre_relay_egress(
        tool_name,
        args,
        {"session_id": session_id, "task_id": task_id, "tool_call_id": tool_call_id},
        enabled=enabled,
    )
    if not enabled:
        return RelayPreparation(dict(args))
    if not decision.allow:
        return RelayPreparation(dict(args), blocked_reason=decision.reason)
    try:
        candidate = _runtime_candidate(
            tool_name,
            args,
            session_id=session_id,
            metadata=decision.payload,
        )
        return RelayPreparation(candidate, trace={"source": "relay", "reason": "candidate_validated"})
    except RelayBlockedError as exc:
        return RelayPreparation(dict(args), trace={"source": "relay", "reason": exc.reason}, blocked_reason=exc.reason)


def execute(
    tool_name: str,
    args: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    session_id: str,
    task_id: str = "",
    tool_call_id: str = "",
    enabled: bool = False,
    phase: str = "dispatch",
    prevalidated: bool = False,
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Execute the callback once, optionally through the claim boundary."""
    del metadata
    if phase == "prepare":
        prepared = prepare(
            tool_name,
            args,
            session_id=session_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            enabled=enabled,
        )
        if prepared.blocked_reason:
            raise RelayBlockedError(prepared.blocked_reason)
        return prepared.args, prepared.args
    if not enabled:
        return callback(args), args
    if not prevalidated:
        prepared = prepare(
            tool_name,
            args,
            session_id=session_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            enabled=enabled,
        )
        if prepared.blocked_reason:
            raise RelayBlockedError(prepared.blocked_reason)
        args = prepared.args
    claim = claim_execution(session_id, task_id, tool_call_id)
    try:
        return callback(args), args
    finally:
        release_execution(claim)


def reset_claims_for_tests() -> None:
    with _CLAIM_LOCK:
        _CLAIMS.clear()
