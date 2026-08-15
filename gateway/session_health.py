"""Deterministic, advisory-only gateway session health checks.

The evaluator is intentionally independent of the model and never mutates a
conversation.  Callers may append the returned suggestion after a successful
turn and persist ``next_state`` in the routing entry's metadata.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


_SOFT_MESSAGE = (
    "💡 Session tip: This conversation has become quite large. If your next "
    "request is about a different topic, send /new first for the clearest "
    "response. Continue here for follow-ups. /new starts a fresh chat context; "
    "it does not delete this session or your saved memory."
)
_STRONG_MESSAGE = (
    "⚠️ Session health: This conversation is now very large and complex. Please "
    "send /new before starting a different task. Continue here only for a direct "
    "follow-up. /new starts a fresh chat context; it does not delete this "
    "session or your saved memory."
)


@dataclass(frozen=True)
class SessionHealthDecision:
    """One pure evaluation result plus state the gateway should persist."""

    should_suggest: bool
    level: str
    message: str
    signals: tuple[str, ...]
    next_state: dict[str, int | float]


@dataclass(frozen=True)
class SessionHealthDelivery:
    """How the gateway should surface advice for one completed turn."""

    response: str
    trailing_message: str


def session_health_can_deliver(
    *, response: str, already_sent: bool, intentional_silence: bool
) -> bool:
    """Return whether a completed turn has client-visible text to advise after."""

    return not intentional_silence and bool(response or already_sent)


def combine_session_health_trailing(footer: str, advice: str) -> str:
    """Combine optional streamed footer and advice into one trailing message."""

    return "\n\n".join(part for part in (footer, advice) if part)


def plan_session_health_delivery(
    *, response: str, advice: str, already_sent: bool
) -> SessionHealthDelivery:
    """Append advice normally or return it as a trailing streaming message."""

    if not advice:
        return SessionHealthDelivery(response=response, trailing_message="")
    if already_sent:
        return SessionHealthDelivery(response=response, trailing_message=advice)
    if not response:
        return SessionHealthDelivery(response=response, trailing_message="")
    return SessionHealthDelivery(
        response=f"{response}\n\n{advice}", trailing_message=""
    )


def _nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_ratio(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _state_value(state: Mapping[str, Any], key: str) -> int:
    return _nonnegative_int(state.get(key), 0)


def _state_timestamp(state: Mapping[str, Any], key: str) -> float:
    value = state.get(key)
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp >= 0.0 else 0.0


def _disabled_decision(state: Mapping[str, Any] | None) -> SessionHealthDecision:
    raw = state if isinstance(state, Mapping) else {}
    next_state: dict[str, int | float] = {
        key: (
            _state_timestamp(raw, key)
            if key == "last_suggested_at"
            else _state_value(raw, key)
        )
        for key in (
            "suggestion_count",
            "last_suggested_at",
            "failure_streak",
            "compression_count",
        )
        if key in raw
    }
    return SessionHealthDecision(
        should_suggest=False,
        level="",
        message="",
        signals=(),
        next_state=next_state,
    )


def _resolve_config(user_config: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(user_config, Mapping):
        return None
    gateway = user_config.get("gateway")
    if not isinstance(gateway, Mapping):
        return None
    health = gateway.get("session_health")
    if not isinstance(health, Mapping):
        return None
    if health.get("enabled") is not True:
        return None
    return health


def count_session_activity(messages: Any) -> tuple[int, int]:
    """Count conversational messages and completed tool calls.

    System prompts and ``session_meta`` rows do not represent client-visible
    conversation growth.  Tool-result rows count completed calls exactly once;
    their matching assistant ``tool_calls`` declarations are not double-counted.
    Malformed entries are ignored.
    """

    if not isinstance(messages, (list, tuple)):
        return 0, 0
    message_count = 0
    tool_call_count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role in {"user", "assistant"}:
            message_count += 1
        elif role == "tool":
            tool_call_count += 1
    return message_count, tool_call_count


def session_health_turn_failed(
    agent_result: Mapping[str, Any] | None,
    final_response: str,
    *,
    gateway_error: bool = False,
) -> bool:
    """Classify non-successful or zero-output turns as failed for advice."""

    if gateway_error or not isinstance(agent_result, Mapping):
        return True
    if any(bool(agent_result.get(key)) for key in ("failed", "interrupted", "partial")):
        return True
    if agent_result.get("completed") is False:
        return True
    if bool(str(agent_result.get("error") or "").strip()):
        return True
    if bool(agent_result.get("already_sent")):
        return False
    visible = str(final_response or "").strip()
    return not visible or visible == "(empty)"


def evaluate_session_health(
    *,
    user_config: Mapping[str, Any] | None,
    platform_key: str,
    message_count: int,
    tool_call_count: int,
    session_age_seconds: int,
    prompt_tokens: int,
    context_length: int,
    agent_failed: bool,
    can_deliver: bool,
    compressed: bool,
    state: Mapping[str, Any] | None,
    now: float,
) -> SessionHealthDecision:
    """Return an optional `/new` suggestion without resetting anything.

    At least two independently configured signals are required.  Failed turns
    update the persisted failure streak but never add advisory text to an error
    response.  Cooldown and maximum counts live in ``next_state`` so callers can
    persist them across gateway restarts.
    """

    config = _resolve_config(user_config)
    if config is None:
        return _disabled_decision(state)

    if platform_key.strip().lower() != "telegram":
        return _disabled_decision(state)

    raw_state = state if isinstance(state, Mapping) else {}
    suggestion_count = _state_value(raw_state, "suggestion_count")
    last_suggested_at = _state_timestamp(raw_state, "last_suggested_at")
    previous_failure_streak = _state_value(raw_state, "failure_streak")
    compression_count = _state_value(raw_state, "compression_count") + int(
        bool(compressed)
    )
    failure_streak = previous_failure_streak + 1 if agent_failed else 0

    next_state: dict[str, int | float] = {
        "suggestion_count": suggestion_count,
        "last_suggested_at": last_suggested_at,
        "failure_streak": failure_streak,
        "compression_count": compression_count,
    }

    min_messages = _nonnegative_int(config.get("min_messages"), 80)
    min_tool_calls = _nonnegative_int(config.get("min_tool_calls"), 25)
    min_age_seconds = _nonnegative_int(config.get("min_age_seconds"), 129_600)
    min_prompt_tokens = _nonnegative_int(config.get("min_prompt_tokens"), 72_000)
    min_context_ratio = _bounded_ratio(config.get("min_context_ratio"), 0.45)
    min_compressions = _nonnegative_int(config.get("min_compressions"), 2)
    min_failure_streak = _nonnegative_int(config.get("min_failure_streak"), 2)
    min_signals = max(2, _nonnegative_int(config.get("min_signals"), 2))
    strong_signals = max(
        3, min_signals, _nonnegative_int(config.get("strong_signals"), 3)
    )
    cooldown_seconds = max(
        86_400, _nonnegative_int(config.get("cooldown_seconds"), 86_400)
    )
    configured_max_suggestions = _nonnegative_int(config.get("max_suggestions"), 2)
    max_suggestions = (
        0 if configured_max_suggestions == 0 else min(2, configured_max_suggestions)
    )

    signals: list[str] = []
    if min_messages > 0 and _nonnegative_int(message_count, 0) >= min_messages:
        signals.append("long_conversation")
    if min_tool_calls > 0 and _nonnegative_int(tool_call_count, 0) >= min_tool_calls:
        signals.append("tool_heavy")
    if (
        min_age_seconds > 0
        and _nonnegative_int(session_age_seconds, 0) >= min_age_seconds
    ):
        signals.append("old_session")

    safe_prompt_tokens = _nonnegative_int(prompt_tokens, 0)
    safe_context_length = _nonnegative_int(context_length, 0)
    context_pressure = min_prompt_tokens > 0 and safe_prompt_tokens >= min_prompt_tokens
    if safe_context_length > 0 and min_context_ratio > 0:
        context_pressure = context_pressure or (
            safe_prompt_tokens / safe_context_length >= min_context_ratio
        )
    if context_pressure:
        signals.append("context_pressure")
    if min_compressions > 0 and compression_count >= min_compressions:
        signals.append("compression")
    if (
        min_failure_streak > 0
        and max(previous_failure_streak, failure_streak) >= min_failure_streak
    ):
        signals.append("recent_failures")

    decision_signals = tuple(signals)
    if (
        agent_failed
        or not can_deliver
        or len(signals) < min_signals
        or max_suggestions == 0
    ):
        return SessionHealthDecision(False, "", "", decision_signals, next_state)
    if suggestion_count >= max_suggestions:
        return SessionHealthDecision(False, "", "", decision_signals, next_state)
    if suggestion_count > 0 and len(signals) < strong_signals:
        return SessionHealthDecision(False, "", "", decision_signals, next_state)
    if suggestion_count > 0 and float(now) - last_suggested_at < cooldown_seconds:
        return SessionHealthDecision(False, "", "", decision_signals, next_state)

    level = "strong" if suggestion_count > 0 else "soft"
    message = _STRONG_MESSAGE if level == "strong" else _SOFT_MESSAGE
    next_state["suggestion_count"] = suggestion_count + 1
    next_state["last_suggested_at"] = max(0.0, float(now))
    return SessionHealthDecision(True, level, message, decision_signals, next_state)
