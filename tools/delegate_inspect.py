"""Bounded live-subagent inspect subsystem.

Single owner of the privacy boundary for ``delegate_task(action="inspect")``:

- ``DelegateEvent`` / ``_LEGACY_EVENT_MAP`` — the delegation event model the
  inspect tee normalizes incoming child events against.
- Argument summarization and target sanitization — keeps bounded side-effect
  targets while dropping credentials. URLs are reduced to **scheme +
  validated authority**: the path is dropped by default because popular
  services embed secrets there (Slack incoming-webhook URLs, Telegram bot
  tokens in ``/bot<token>/`` paths). Re-introducing any path-preserving
  exception requires an explicit entry in
  ``_TOOL_INPUT_PATH_PRESERVING_HOSTS`` **plus its own adversarial
  evidence**; the allowlist ships empty (fail closed).
- Bounded per-child inspect ring, degradation counter, canonical tool
  counters, and the strict JSON serializer for the inspect response.

This module deliberately holds no state of its own: the live registry and
its lock are owned by ``tools.delegate_tool`` and injected once via
``bind_registry()`` at import time (no circular import).
"""

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delegation event model
# ---------------------------------------------------------------------------

import enum


class DelegateEvent(str, enum.Enum):
    """Formal event types emitted during delegation progress.

    ``_LEGACY_EVENT_MAP`` normalises incoming legacy strings
    (``tool.started``, ``_thinking``, …) to these enum values. External
    consumers (gateway SSE, ACP adapter, CLI) still receive the legacy
    strings during the deprecation window.

    TASK_SPAWNED / TASK_COMPLETED / TASK_FAILED are reserved for future
    orchestrator lifecycle events and are not currently emitted.
    """

    TASK_SPAWNED = "delegate.task_spawned"
    TASK_PROGRESS = "delegate.task_progress"
    TASK_COMPLETED = "delegate.task_completed"
    TASK_FAILED = "delegate.task_failed"
    TASK_THINKING = "delegate.task_thinking"
    TASK_TOOL_STARTED = "delegate.tool_started"
    TASK_TOOL_COMPLETED = "delegate.tool_completed"


# Legacy event strings → DelegateEvent mapping.
# Incoming child-agent events use the old names; callbacks normalise them.
_LEGACY_EVENT_MAP: Dict[str, DelegateEvent] = {
    "_thinking": DelegateEvent.TASK_THINKING,
    "reasoning.available": DelegateEvent.TASK_THINKING,
    "tool.started": DelegateEvent.TASK_TOOL_STARTED,
    "tool.completed": DelegateEvent.TASK_TOOL_COMPLETED,
    "subagent_progress": DelegateEvent.TASK_PROGRESS,
}


# ---------------------------------------------------------------------------
# Registry binding (owned by tools.delegate_tool)
# ---------------------------------------------------------------------------

_registry_lock: Optional[threading.Lock] = None
_registry: Optional[Dict[str, Dict[str, Any]]] = None


def bind_registry(lock: threading.Lock, registry: Dict[str, Dict[str, Any]]) -> None:
    """Inject the live subagent registry this subsystem records into.

    Called exactly once by ``tools.delegate_tool`` at import time. The
    mapping object is shared by reference; the registry never rebinds it.
    """
    global _registry_lock, _registry
    _registry_lock = lock
    _registry = registry


def _registry_ref() -> Any:
    """Context manager yielding ``(lock, registry)`` or ``(None, None)``."""
    return _registry_lock, _registry


# ---------------------------------------------------------------------------
# Argument summarization / sanitization — the privacy boundary
# ---------------------------------------------------------------------------

_TOOL_INPUT_TARGET_KEYS = frozenset({
    "cwd",
    "destination_path",
    "directory",
    "dst",
    "endpoint",
    "file_path",
    "new_path",
    "old_path",
    "path",
    "source_path",
    "src",
    "target_path",
    "url",
    "urls",
})
_TOOL_INPUT_URL_KEYS = frozenset({"endpoint", "url", "urls"})

# Hosts for which a sanitized URL may retain its raw path segment. Ships
# EMPTY on purpose: generic URL-like targets fail closed to scheme +
# validated authority because credential-bearing paths (Slack webhook
# tokens, Telegram bot tokens) must never reach the parent model through
# action='inspect'. Adding an entry requires tool-specific adversarial
# evidence proving the path is not secret-bearing.
_TOOL_INPUT_PATH_PRESERVING_HOSTS: frozenset = frozenset()


def sanitize_url_target(value: str) -> Optional[str]:
    """Reduce a URL to scheme + validated authority, dropping everything else.

    Userinfo, query, fragment, AND path are removed: the path is a common
    secret-carrier (hooks.slack.com/services/<token>,
    api.telegram.org/bot<token>/...) and cannot be model-exposed.
    Returns None when the value is not a parseable absolute URL or the
    result would leak anything beyond a bare origin.
    """
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            hostname = parsed.hostname
            if not hostname:
                return None
            host = f"[{hostname}]" if ":" in hostname else hostname
            port = parsed.port
            netloc = f"{host}:{port}" if port is not None else host
            if hostname.lower() in _TOOL_INPUT_PATH_PRESERVING_HOSTS:
                return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
            # Default: origin only. Nothing about the original request
            # location survives except scheme + host[:port].
            return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except ValueError:
        return None
    return None


def sanitize_target(key: str, value: Any) -> Any:
    """Keep bounded side-effect targets while dropping URL secrets."""
    if isinstance(value, list):
        cleaned = [
            item for item in (sanitize_target(key, item) for item in value[:16])
            if item is not None
        ]
        return cleaned or None
    if not isinstance(value, str) or not value:
        return None
    bounded = value[:1024]
    if key in _TOOL_INPUT_URL_KEYS:
        return sanitize_url_target(bounded)
    return bounded


def summarize_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Summarize argument names and side-effect targets without raw payloads."""
    if not isinstance(arguments, str):
        return {"argument_keys": [], "targets": {}}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {"argument_keys": [], "targets": {}}
    if not isinstance(parsed, dict):
        return {"argument_keys": [], "targets": {}}

    keys = sorted(str(key)[:128] for key in parsed)[:64]
    targets: Dict[str, Any] = {}
    for raw_key, value in parsed.items():
        key = str(raw_key).lower()
        if key not in _TOOL_INPUT_TARGET_KEYS:
            continue
        cleaned = sanitize_target(key, value)
        if cleaned is not None:
            targets[key] = cleaned
    return {"argument_keys": keys, "targets": targets}


def sanitize_input_summary(summary: Any) -> Dict[str, Any]:
    if not isinstance(summary, dict):
        return {"argument_keys": [], "targets": {}}
    keys = summary.get("argument_keys")
    safe_keys = (
        [str(key)[:128] for key in keys[:64]]
        if isinstance(keys, list)
        else []
    )
    targets = summary.get("targets")
    safe_targets: Dict[str, Any] = {}
    if isinstance(targets, dict):
        for raw_key, value in targets.items():
            key = str(raw_key).lower()
            if key not in _TOOL_INPUT_TARGET_KEYS:
                continue
            cleaned = sanitize_target(key, value)
            if cleaned is not None:
                safe_targets[key] = cleaned
    return {"argument_keys": safe_keys, "targets": safe_targets}


# ---------------------------------------------------------------------------
# Bounded inspect ring / telemetry capture
# ---------------------------------------------------------------------------

SUBAGENT_INSPECT_EVENT_LIMIT = 12
_SUBAGENT_INSPECT_CAPTURE_ERROR_LIMIT = 2_147_483_647


def record_tool_started(subagent_id: str, tool_name: Any) -> None:
    """Update the existing canonical live-registry tool activity fields."""
    if _registry is None or _registry_lock is None:
        return
    safe_tool = str(tool_name or "unknown")[:256]
    with _registry_lock:
        record = _registry.get(subagent_id)
        if record is None:
            return
        count = record.get("tool_count", 0)
        if isinstance(count, bool) or not isinstance(count, int):
            count = 0
        record["tool_count"] = max(0, count) + 1
        record["last_tool"] = safe_tool


def record_capture_error(subagent_id: str) -> None:
    """Mark supplementary inspect capture degraded without retaining error text."""
    if _registry is None or _registry_lock is None:
        return
    with _registry_lock:
        record = _registry.get(subagent_id)
        if record is None:
            return
        value = record.get("_inspect_capture_errors", 0)
        if isinstance(value, bool) or not isinstance(value, int):
            value = 0
        record["_inspect_capture_errors"] = min(
            _SUBAGENT_INSPECT_CAPTURE_ERROR_LIMIT, max(0, value) + 1
        )


def append_inspect_event(subagent_id: str, event: Dict[str, Any]) -> None:
    """Append one bounded metadata-only event to a live child's inspect ring.

    This is deliberately not a transcript: reasoning, assistant text, raw
    arguments, and raw tool results never enter this ring.
    """
    kind = str(event.get("type") or "")
    if kind not in {"tool_started", "tool_completed"}:
        return
    tool_name = str(event.get("tool") or "unknown")[:256]
    safe_event: Dict[str, Any] = {"type": kind, "tool": tool_name}
    if kind == "tool_started":
        safe_event["tool_input"] = sanitize_input_summary(
            event.get("tool_input")
        )
    else:
        status = str(event.get("status") or "unknown").lower()
        safe_event["status"] = status if status in {"ok", "error"} else "unknown"
        duration = event.get("duration_seconds")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(float(duration))
        ):
            safe_event["duration_seconds"] = round(max(0.0, float(duration)), 3)

    if _registry is None or _registry_lock is None:
        return
    with _registry_lock:
        record = _registry.get(subagent_id)
        if record is None:
            return
        events = record.setdefault("_inspect_events", [])
        if not isinstance(events, list):
            events = []
            record["_inspect_events"] = events
        events.append(safe_event)
        if len(events) > SUBAGENT_INSPECT_EVENT_LIMIT:
            del events[:-SUBAGENT_INSPECT_EVENT_LIMIT]


def wrap_inspect_callback(inner_cb: Any, subagent_id: str) -> Any:
    """Tee tool lifecycle into a bounded model-safe inspect ring.

    The tee is always installed for live children, including headless hosts, so
    canonical registry tool_count/last_tool no longer depend on a display
    callback. Supplementary ring-capture failures never break execution; they
    increment a bounded degradation counter surfaced by action='inspect'.
    """

    def _cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
        try:
            if isinstance(event_type, DelegateEvent):
                event = event_type
            else:
                event = _LEGACY_EVENT_MAP.get(event_type)
                if event is None:
                    try:
                        event = DelegateEvent(event_type)
                    except (ValueError, TypeError):
                        event = None

            if event == DelegateEvent.TASK_TOOL_STARTED:
                # Update canonical live activity before optional argument
                # summarization so a sanitizer failure cannot lose the count.
                record_tool_started(subagent_id, tool_name)
                if isinstance(args, str):
                    serialized_args = args
                elif args is None:
                    serialized_args = ""
                else:
                    serialized_args = json.dumps(
                        args, ensure_ascii=False, default=str
                    )
                append_inspect_event(
                    subagent_id,
                    {
                        "type": "tool_started",
                        "tool": tool_name,
                        "tool_input": summarize_tool_arguments(serialized_args),
                    },
                )
            elif event == DelegateEvent.TASK_TOOL_COMPLETED:
                append_inspect_event(
                    subagent_id,
                    {
                        "type": "tool_completed",
                        "tool": tool_name,
                        "status": "error" if kwargs.get("is_error") else "ok",
                        "duration_seconds": kwargs.get("duration"),
                    },
                )
        except Exception:
            record_capture_error(subagent_id)
            logger.debug(
                "Subagent inspect capture failed for %s", subagent_id, exc_info=True
            )

        # Preserve the original callback's exception semantics: only inspect
        # capture is isolated; existing display/transcript callback failures are
        # handled exactly where they were before this tee existed.
        if inner_cb is not None:
            return inner_cb(event_type, tool_name, preview, args, **kwargs)
        return None

    def _flush():
        inner_flush = getattr(inner_cb, "_flush", None)
        if callable(inner_flush):
            return inner_flush()
        return None

    _cb._flush = _flush  # type: ignore[attr-defined]  # mirrors inner_cb contract
    return _cb


# ---------------------------------------------------------------------------
# Inspect response normalization + strict serialization
# ---------------------------------------------------------------------------


def nonnegative_int(value: Any) -> int:
    """Coerce a counter to a finite non-negative int; anything else -> 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(numeric) or numeric < 0:
        return 0
    return int(numeric)


def bounded_tool_label(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[:256]


def finite_seconds(value: Any, *, relative_to: Optional[float] = None) -> Optional[float]:
    """Finite rounded duration; optionally expressed as elapsed-since."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        seconds = max(0.0, float(value))
        if relative_to is not None:
            seconds = max(0.0, relative_to - seconds)
        return round(seconds, 1)
    return None


def serialize_inspect_response(
    base_snapshot: Dict[str, Any],
    agent: Any,
    activity_raw: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> str:
    """Build the strict-JSON inspect payload from sampled live evidence.

    Every counter is normalized to a finite non-negative integer; NaN /
    Infinity can never reach the parent model (``allow_nan=False`` makes
    serialization fail loudly rather than emit junk).
    """
    if now is None:
        now = time.time()

    last_activity_ts = activity_raw.get("last_activity_ts")
    seconds_since_activity = finite_seconds(last_activity_ts, relative_to=now)

    running_seconds = finite_seconds(base_snapshot.get("started_at"), relative_to=now)

    cost_value = getattr(agent, "session_estimated_cost_usd", 0.0)
    try:
        cost_float = float(cost_value)
        estimated_cost_usd = (
            max(0.0, cost_float) if math.isfinite(cost_float) else 0.0
        )
    except (TypeError, ValueError, OverflowError):
        estimated_cost_usd = 0.0

    capture_errors = nonnegative_int(base_snapshot.get("capture_errors"))
    return json.dumps(
        {
            "action": "inspect",
            "subagent_id": base_snapshot.get("subagent_id"),
            "goal": base_snapshot.get("goal"),
            "model": base_snapshot.get("model"),
            "status": base_snapshot.get("status"),
            "running_seconds": running_seconds,
            "activity": {
                "current_tool": bounded_tool_label(
                    activity_raw.get("current_tool")
                ),
                "last_tool": bounded_tool_label(base_snapshot.get("last_tool")),
                "tool_count": nonnegative_int(base_snapshot.get("tool_count")),
                "api_calls": nonnegative_int(activity_raw.get("api_call_count", 0)),
                "max_iterations": nonnegative_int(
                    activity_raw.get("max_iterations", 0)
                ),
                "seconds_since_activity": seconds_since_activity,
            },
            "usage": {
                "input_tokens": nonnegative_int(
                    getattr(agent, "session_prompt_tokens", 0)
                ),
                "output_tokens": nonnegative_int(
                    getattr(agent, "session_completion_tokens", 0)
                ),
                "estimated_cost_usd": round(estimated_cost_usd, 6),
            },
            "recent_events": base_snapshot.get("recent_events", []),
            "telemetry": {
                "capture_degraded": capture_errors > 0,
                "capture_errors": capture_errors,
                "recent_event_limit": SUBAGENT_INSPECT_EVENT_LIMIT,
            },
            "accepting_steer": base_snapshot.get("accepting_steer", False),
        },
        ensure_ascii=False,
        allow_nan=False,
    )
