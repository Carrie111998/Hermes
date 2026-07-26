"""Local, opt-in DecisionTrace observer for Hermes.

This plugin is intentionally metadata-only: it never stores prompts, model
responses, tool arguments, tool results, credentials, or arbitrary payloads.
It writes one compact JSON object per completed LLM turn to
``$HERMES_HOME/telemetry/decision-traces.jsonl``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_TURNS: dict[str, dict[str, Any]] = {}
_MAX_LIVE_TURNS = 256
_LOGGER = logging.getLogger(__name__)


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _path() -> Path:
    return _home() / "telemetry" / "decision-traces.jsonl"


def _key(task_id: str, session_id: str, turn_id: str, api_request_id: str) -> str:
    if turn_id:
        return f"turn:{turn_id}"
    if api_request_id:
        return f"api:{api_request_id}"
    return f"task:{task_id}:session:{session_id}"


def _bounded_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_status(finish_reason: Any, assistant_tool_call_count: Any) -> str:
    if _bounded_int(assistant_tool_call_count):
        return "tool_call"
    return str(finish_reason or "completed")[:64]


def _get_or_create(key: str, **fields: Any) -> dict[str, Any]:
    state = _TURNS.get(key)
    if state is None:
        state = {
            "trace_id": uuid.uuid4().hex,
            "started_at": time.time(),
            "api_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
        _TURNS[key] = state
    for name in ("task_id", "session_id", "turn_id", "model", "provider", "api_mode"):
        value = fields.get(name)
        if value and not state.get(name):
            state[name] = str(value)[:256]
    if len(_TURNS) > _MAX_LIVE_TURNS:
        oldest = min(_TURNS, key=lambda item: _TURNS[item].get("started_at", 0.0))
        _TURNS.pop(oldest, None)
    return state


def on_pre_api_request(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                       api_request_id: str = "", model: str = "", provider: str = "",
                       api_mode: str = "", **_: Any) -> None:
    key = _key(task_id, session_id, turn_id, api_request_id)
    with _LOCK:
        state = _get_or_create(
            key, task_id=task_id, session_id=session_id, turn_id=turn_id,
            model=model, provider=provider, api_mode=api_mode,
        )
        state["api_calls"] += 1
        state["last_api_request_id"] = str(api_request_id or "")[:256]


def on_post_api_request(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                        api_request_id: str = "", model: str = "", provider: str = "",
                        api_mode: str = "", usage: Any = None, **_: Any) -> None:
    key = _key(task_id, session_id, turn_id, api_request_id)
    usage = usage if isinstance(usage, dict) else {}
    with _LOCK:
        state = _get_or_create(
            key, task_id=task_id, session_id=session_id, turn_id=turn_id,
            model=model, provider=provider, api_mode=api_mode,
        )
        state["input_tokens"] += _bounded_int(usage.get("input_tokens"))
        state["output_tokens"] += _bounded_int(usage.get("output_tokens", usage.get("completion_tokens")))
        state["cache_read_tokens"] += _bounded_int(usage.get("cache_read_tokens"))
        state["cache_write_tokens"] += _bounded_int(usage.get("cache_write_tokens"))
        try:
            state["estimated_cost_usd"] += max(0.0, float(usage.get("estimated_cost_usd") or 0.0))
        except (TypeError, ValueError):
            pass


def on_post_tool_call(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                      api_request_id: str = "", **_: Any) -> None:
    key = _key(task_id, session_id, turn_id, api_request_id)
    with _LOCK:
        state = _get_or_create(key, task_id=task_id, session_id=session_id, turn_id=turn_id)
        state["tool_calls"] += 1


def _write(state: dict[str, Any], *, finish_reason: Any = "", assistant_tool_call_count: Any = 0,
           api_duration: Any = 0.0) -> None:
    now = time.time()
    record = {
        "schema_version": "hermes.decision_trace.v1",
        "trace_id": state["trace_id"],
        "task_id": state.get("task_id", ""),
        "session_id": state.get("session_id", ""),
        "turn_id": state.get("turn_id", ""),
        "model": state.get("model", ""),
        "provider": state.get("provider", ""),
        "api_mode": state.get("api_mode", ""),
        "started_at": state["started_at"],
        "ended_at": now,
        "duration_ms": round(max(0.0, now - state["started_at"]) * 1000),
        "api_duration_ms": round(max(0.0, float(api_duration or 0.0)) * 1000),
        "status": _safe_status(finish_reason, assistant_tool_call_count),
        "api_calls": state["api_calls"],
        "tool_calls": state["tool_calls"] + _bounded_int(assistant_tool_call_count),
        "input_tokens": state["input_tokens"],
        "output_tokens": state["output_tokens"],
        "cache_read_tokens": state["cache_read_tokens"],
        "cache_write_tokens": state["cache_write_tokens"],
        "estimated_cost_usd": round(state["estimated_cost_usd"], 8),
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def on_post_llm_call(*, task_id: str = "", session_id: str = "", turn_id: str = "",
                     api_request_id: str = "", finish_reason: str = "", usage: Any = None,
                     assistant_tool_call_count: int = 0, api_duration: float = 0.0,
                     model: str = "", provider: str = "", api_mode: str = "", **_: Any) -> None:
    key = _key(task_id, session_id, turn_id, api_request_id)
    with _LOCK:
        state = _TURNS.pop(key, None)
        if state is None:
            state = _get_or_create(
                key, task_id=task_id, session_id=session_id, turn_id=turn_id,
                model=model, provider=provider, api_mode=api_mode,
            )
        # API-scoped hooks normally account for usage already. Only use the
        # turn-scoped usage payload as a fallback when no API request was seen,
        # avoiding double counting on current Hermes paths.
        if state["api_calls"] == 0 and isinstance(usage, dict):
            state["input_tokens"] += _bounded_int(usage.get("input_tokens"))
            state["output_tokens"] += _bounded_int(usage.get("output_tokens", usage.get("completion_tokens")))
        try:
            _write(state, finish_reason=finish_reason, assistant_tool_call_count=assistant_tool_call_count,
                   api_duration=api_duration)
        except Exception as exc:  # observer must never break the agent loop
            _LOGGER.debug("DecisionTrace write failed: %s", exc)


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
