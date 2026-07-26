"""One-shot doctrine route context for a routed Hermes child process."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_route_context: dict[str, Any] | None = None
_read_attempted = False
_failure_history: list[dict[str, Any]] = []
_flushed = False
_cascade_exhausted = False
_final_failure_class: str | None = None
_STATE_LOCK = threading.RLock()


def _valid_fallback_chain(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("provider"), str)
        and bool(entry["provider"].strip())
        and isinstance(entry.get("model"), str)
        and bool(entry["model"].strip())
        for entry in value
    )


def _validate_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    decision_row_id = value.get("decision_row_id")
    if value.get("schema_version") != 1:
        return None
    if not isinstance(decision_row_id, int) or isinstance(
        decision_row_id, bool
    ):
        return None
    if not _valid_fallback_chain(value.get("fallback_chain")):
        return None
    return dict(value)


def get_route_context() -> dict[str, Any] | None:
    """Read and clear ``HERMES_ROUTE_CONTEXT_JSON`` exactly once."""
    global _read_attempted, _route_context
    with _STATE_LOCK:
        if _read_attempted:
            os.environ.pop("HERMES_ROUTE_CONTEXT_JSON", None)
            return _route_context
        _read_attempted = True
        raw = os.environ.pop("HERMES_ROUTE_CONTEXT_JSON", None)
        if not raw:
            return None
        try:
            _route_context = _validate_context(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            _route_context = None
        return _route_context


def append_failure(
    *,
    provider: str,
    model: str,
    failure_class: str,
    latency_ms: int,
    error_repr: str,
    transition_reason: str,
) -> None:
    """Append one provider-switch failure to the in-memory cascade history."""
    entry = {
        "provider": str(provider),
        "model": str(model),
        "failure_class": str(failure_class or "unknown"),
        "latency_ms": max(0, int(latency_ms or 0)),
        "attempt_ts": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "error_repr": str(error_repr)[:500],
        "transition_reason": str(transition_reason),
    }
    with _STATE_LOCK:
        _failure_history.append(entry)


def failure_history_snapshot() -> list[dict[str, Any]]:
    """Return a copy safe for persistence and cap checks."""
    with _STATE_LOCK:
        return [dict(entry) for entry in _failure_history]


def mark_cascade_exhausted(
    final_failure_class: str | None = None,
) -> None:
    """Mark the current doctrine chain as terminally exhausted."""
    global _cascade_exhausted, _final_failure_class
    with _STATE_LOCK:
        _cascade_exhausted = True
        if final_failure_class:
            _final_failure_class = str(final_failure_class)


def is_cascade_exhausted() -> bool:
    with _STATE_LOCK:
        return bool(_cascade_exhausted)


def _record_cascade_verdict(
    context: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    db_path: str | Path | None,
) -> None:
    task_id = str(context.get("task_id") or "").strip()
    if not task_id:
        return

    from hermes_cli.verdict import LeafVerdict, record_verdict
    from hermes_cli.verdict.types import canonical_strategy_hash

    chain_length = len(context.get("fallback_chain") or [])
    final_class = (
        _final_failure_class
        or (history[-1].get("failure_class") if history else None)
        or "unknown"
    )
    raw_meta = {
        "cascade_exhausted": True,
        "chain_length": chain_length,
        "final_failure_class": str(final_class),
        "primary_provider": context.get("primary_provider"),
        "primary_model": context.get("primary_model"),
        "decision_row_id": int(context["decision_row_id"]),
    }
    record_verdict(
        LeafVerdict(
            task_id=task_id,
            attempt_number=max(1, len(history) + 1),
            rung_id="r0_baseline",
            model_used="__none__",
            outcome="failure",
            failure_class="infra",
            failure_signals=["cascade_exhausted"],
            confidence=0.0,
            strategy_hash=canonical_strategy_hash(raw_meta),
            error_class="CascadeExhausted",
            error_message="all doctrine fallback providers failed",
            raw_meta=raw_meta,
        ),
        db_path=db_path,
        profile=None,
        route="single",
        session_id=context.get("session_id"),
    )


def flush_to_db(
    *,
    chosen_provider: str,
    chosen_model: str,
    outcome: str,
    db_path: str | Path | None = None,
) -> bool:
    """Persist route outcome once; return whether a flush was performed."""
    global _flushed
    context = get_route_context()
    with _STATE_LOCK:
        if context is None or _flushed:
            return False
        history = [dict(entry) for entry in _failure_history]
        all_failed = bool(_cascade_exhausted)

    provider = str(chosen_provider)
    model = str(chosen_model)
    if all_failed:
        provider = "__all_failed__"
        model = "__none__"
        outcome = "failure"

    from hermes_cli.routing.facade import persist_fallback_result

    persist_fallback_result(
        decision_row_id=int(context["decision_row_id"]),
        failure_history=history,
        chosen_provider=provider,
        chosen_model=model,
        db_path=db_path,
    )
    if all_failed and str(outcome) == "failure":
        _record_cascade_verdict(context, history, db_path=db_path)

    with _STATE_LOCK:
        _flushed = True
    return True


def _reset_for_tests() -> None:
    """Reset process globals for hermetic tests."""
    global _route_context, _read_attempted, _flushed
    global _cascade_exhausted, _final_failure_class
    with _STATE_LOCK:
        _route_context = None
        _read_attempted = False
        _failure_history.clear()
        _flushed = False
        _cascade_exhausted = False
        _final_failure_class = None


__all__ = [
    "_failure_history",
    "append_failure",
    "failure_history_snapshot",
    "flush_to_db",
    "get_route_context",
    "is_cascade_exhausted",
    "mark_cascade_exhausted",
]
