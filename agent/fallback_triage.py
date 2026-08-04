"""Bounded execution for ``failure_policy: triage_and_notify`` fallbacks.

The policy is intentionally narrow.  It does not switch the live agent's
provider/model/client, replay its conversation, or expose its tool registry to
the local model.  A normal interactive turn is held and finalized through the
existing durable session path.  The existing cron source lane (``platform ==
"cron"``) may make one fixed-prompt, toolless local notification/liveness call
so recurring operational ticks remain schedulable.
"""
from __future__ import annotations

import logging
from typing import Any

from agent.error_classifier import FailoverReason
from hermes_cli.fallback_config import resolve_entry_api_key

logger = logging.getLogger(__name__)

# One fixed low ceiling is deliberately not profile/model configuration.  The
# policy's config surface selects the behavior; the bounded safety envelope is
# invariant across providers and prevents a local notifier from becoming a
# continuation runtime.
TRIAGE_MAX_OUTPUT_TOKENS = 256
TRIAGE_REQUEST_TIMEOUT_SECONDS = 20


# No original user content, transcript, system prompt, tools, or provider error
# payload is permitted in either message.  This preserves the primary context
# cache/alternation untouched and keeps a 272K primary turn out of a smaller
# local context window.
_BOUNDED_TRIAGE_MESSAGES = (
    {
        "role": "system",
        "content": (
            "You are a bounded local incident-triage notifier. Return one short "
            "plain-text acknowledgement only. Do not continue any task, use tools, "
            "plan work, make external calls, or infer task details."
        ),
    },
    {
        "role": "user",
        "content": (
            "The primary model is unavailable. The original scheduled task is held. "
            "Acknowledge the held-task notification only."
        ),
    },
)


def _reason_label(reason: FailoverReason | None) -> str:
    value = getattr(reason, "value", reason)
    text = str(value or "provider_failure").strip().lower()
    # A classifier enum is controlled input, but keep a conservative bounded
    # grammar in case this helper is used by an extension with arbitrary text.
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})[:64] or "provider_failure"


def _entry_value(entry: dict[str, Any], key: str) -> str:
    return str(entry.get(key) or "").strip()[:160]


def _normal_hold_response() -> str:
    return (
        "⚠️ Fallback triage-and-notify policy held this turn after the primary "
        "provider failed. The session checkpoint was preserved; no local model, "
        "tool loop, or high-capability continuation was run. Resume the task when "
        "the primary provider is available."
    )


def _scheduled_success_response() -> str:
    return (
        "⚠️ Fallback triage-and-notify policy held the original scheduled task after "
        "the primary provider failed. One bounded, toolless local notification check "
        "completed; no consequential continuation or external side effects were run."
    )


def _scheduled_failure_response() -> str:
    return (
        "⚠️ Fallback triage-and-notify policy held the original scheduled task after "
        "the primary provider failed. The bounded local triage check also failed; no "
        "continuation, tools, or external side effects were run."
    )


def _invalid_policy_response() -> str:
    return (
        "⚠️ Invalid fallback failure_policy configuration held this turn after "
        "the primary provider failed. No fallback client, normal continuation, "
        "tool loop, or external side effect was run. Configure the entry with "
        "failure_policy 'continue' or 'triage_and_notify' before retrying."
    )


def _run_bounded_scheduled_triage(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """Run exactly one isolated local notification/liveness completion.

    This function is deliberately agent-free: it receives only the selected
    fallback entry plus its small policy state and never receives the original
    task, session messages, prompt cache, or tools. Any resolver/call failure is
    explicit held outcome rather than a reason to use the fallback for normal
    agent work.
    """
    provider = _entry_value(entry, "provider")
    model = _entry_value(entry, "model")
    try:
        from agent.auxiliary_client import resolve_provider_client

        base_url = _entry_value(entry, "base_url")
        api_mode = _entry_value(entry, "api_mode") or _entry_value(entry, "transport")
        api_key = resolve_entry_api_key(entry) or ""
        client, resolved_model = resolve_provider_client(
            provider,
            model=model,
            explicit_base_url=base_url,
            explicit_api_key=api_key,
            api_mode=api_mode,
        )
        if client is None:
            raise RuntimeError("local_client_unavailable")

        create = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
        if not callable(create):
            raise RuntimeError("local_client_has_no_chat_completions")

        response = create(
            model=resolved_model or model,
            messages=[dict(message) for message in _BOUNDED_TRIAGE_MESSAGES],
            max_tokens=TRIAGE_MAX_OUTPUT_TOKENS,
            temperature=0,
            timeout=TRIAGE_REQUEST_TIMEOUT_SECONDS,
        )
        choices = getattr(response, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("empty_local_triage_response")
        state["local_triage_succeeded"] = True
    except Exception as exc:
        # Do not preserve raw resolver/provider text in the durable alert: a
        # provider exception may contain headers, endpoints, or credentials.
        state["local_triage_succeeded"] = False
        state["local_triage_error"] = type(exc).__name__
        logger.warning(
            "Bounded fallback triage failed for provider=%s model=%s: %s",
            provider,
            model,
            type(exc).__name__,
        )


def arm_triage_and_notify_hold(
    agent: Any,
    entry: dict[str, Any],
    reason: FailoverReason | None,
) -> dict[str, Any]:
    """Record an immediate alert/hold without switching the live agent runtime."""
    is_scheduled = str(getattr(agent, "platform", "") or "").strip().lower() == "cron"
    reason_label = _reason_label(reason)
    alert = (
        "⚠️ Fallback triage-and-notify activated: the primary provider failed "
        f"({reason_label}); the original task is checkpointed and held. "
        "No local continuation or tools will run."
    )
    state: dict[str, Any] = {
        # Do not retain the raw config entry: it may contain an inline key.
        # The selected entry is consumed only by the immediate bounded cron
        # call below, never by durable session state.
        "reason": reason_label,
        "scheduled": is_scheduled,
        "alert": alert,
        "local_triage_succeeded": None,
        "local_triage_error": None,
    }
    agent._fallback_triage_state = state
    # Never surface a stale successful-continuation notice from an earlier
    # fallback path as if this held turn had switched models.
    agent._pending_fallback_notice = None

    # Reuse the existing status/event channel for the immediate operator alert.
    # The finalizer will persist the deterministic final response as the
    # durable checkpoint/hold record even if a surface does not expose status.
    # Drop ordinary "switching" retry chatter: this policy does not switch
    # the live runtime, and the deterministic alert below is the sole notice.
    try:
        agent._clear_status_buffer()
    except Exception:
        pass
    try:
        agent._emit_status(alert)
    except Exception:
        logger.debug("Fallback triage status delivery failed", exc_info=True)

    if is_scheduled:
        _run_bounded_scheduled_triage(state, entry)
    return state


def arm_invalid_fallback_policy_hold(
    agent: Any,
    reason: FailoverReason | None,
) -> dict[str, Any]:
    """Hold immediately when a present fallback policy is malformed.

    The malformed raw value and full config entry are intentionally excluded
    from state and operator output. This is a deterministic configuration
    failure, not a provider-resolution candidate.
    """
    reason_label = _reason_label(reason)
    alert = _invalid_policy_response()
    state: dict[str, Any] = {
        "reason": reason_label,
        "scheduled": False,
        "policy_invalid": True,
        "alert": alert,
        "local_triage_succeeded": None,
        "local_triage_error": None,
    }
    agent._fallback_triage_state = state
    agent._pending_fallback_notice = None
    try:
        agent._clear_status_buffer()
    except Exception:
        pass
    try:
        agent._emit_status(alert)
    except Exception:
        logger.debug("Invalid fallback policy status delivery failed", exc_info=True)
    return state


def triage_turn_outcome(state: dict[str, Any]) -> tuple[str, bool, str]:
    """Return ``(response, failed, exit_reason)`` for a pending policy hold."""
    if state.get("policy_invalid"):
        return _invalid_policy_response(), True, "fallback_policy_invalid"
    if state.get("scheduled"):
        if state.get("local_triage_succeeded") is True:
            return _scheduled_success_response(), False, "fallback_triage_notified"
        return _scheduled_failure_response(), True, "fallback_triage_local_failed"
    return _normal_hold_response(), True, "fallback_triage_held"
