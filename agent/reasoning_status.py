"""Truthful reporting of whether reasoning effort actually reaches the provider.

``agent.reasoning_effort`` is resolved by ``hermes_constants.resolve_reasoning_config``
and then gated a second time, at the wire, by
``AIAgent._supports_reasoning_extra_body()``. That gate is an allowlist of
routes known to accept a ``reasoning``/``reasoning_effort`` field — direct Nous
Portal, OpenRouter with specific model prefixes, LM Studio, Ollama, GitHub
Models. Everything else returns False.

The gate is deliberately conservative: OpenRouter forwards unknown extra_body
fields upstream and some providers answer 400. The defect is not the gate, it
is the silence. An operator who sets ``reasoning_effort: medium`` against a
self-hosted endpoint (a LiteLLM proxy on 127.0.0.1, say) gets no indication
whatsoever that the setting is inert, and every surface keeps reporting the
configured level as though it were live.

This module supplies the two missing pieces:

* :func:`passthrough_override` — an explicit operator opt-in
  (``agent.reasoning_passthrough``) for endpoints the operator knows accept the
  field. This authorizes a *standard* OpenAI-compatible parameter on a route
  Hermes cannot auto-detect; it never invents a provider-specific field.
* :func:`describe` — structured, side-effect-free truth for diagnostics:
  what was configured, whether it will actually be transmitted, and why not.

Nothing here changes what is sent by default. Default behaviour is unchanged;
only the reporting and the opt-in are new.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Values accepted for agent.reasoning_passthrough.
_TRUE = {"1", "true", "yes", "on", "always", "force"}
_FALSE = {"0", "false", "no", "off", "never"}


def passthrough_override(cfg: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Resolve ``agent.reasoning_passthrough``.

    Returns True (always send), False (never send), or None (auto-detect —
    the historical behaviour). An unrecognised value resolves to None rather
    than guessing, so a typo degrades to the safe default instead of silently
    forcing a field onto a provider that rejects it.
    """
    if not isinstance(cfg, dict):
        return None
    agent_cfg = cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        return None
    raw = agent_cfg.get("reasoning_passthrough")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def configured_effort(cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """The effort the operator asked for, as written in config."""
    if not isinstance(cfg, dict):
        return None
    agent_cfg = cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        return None
    raw = agent_cfg.get("reasoning_effort")
    if raw is None or raw is False:
        return None
    text = str(raw).strip()
    return text or None


def describe(
    *,
    configured: Optional[str],
    supported: bool,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    override: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return a structured, human-readable account of the effective state.

    Pure: takes the already-resolved facts and formats them. Keeping it free
    of agent internals is what lets both the CLI and the tests use it.
    """
    effective = configured if (configured and supported) else None
    status: Dict[str, Any] = {
        "configured_effort": configured,
        "effective_effort": effective,
        "will_be_sent": bool(configured and supported),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "passthrough_override": override,
    }

    if not configured:
        status["summary"] = "no reasoning effort configured; provider default applies"
        status["reason"] = None
        return status

    if supported:
        how = "operator opt-in (agent.reasoning_passthrough)" if override is True \
            else "route auto-detected as reasoning-capable"
        status["summary"] = f"reasoning effort {configured!r} IS sent — {how}"
        # Sent is not the same as honored. A route can accept the field and
        # ignore it: probing this profile's LiteLLM router showed HTTP 200 for
        # every effort level while reasoning-token counts stayed non-monotonic
        # (minimal 56.5 > high 51.0 > control 53.5 > low 37.0, medians of 4),
        # and the control — no field at all — already produced reasoning. Say
        # so rather than let "sent" be read as "in effect".
        status["reason"] = (
            "sent, but acceptance is not proof of effect — a provider may "
            "accept reasoning_effort and ignore it. Confirm with reasoning "
            "token counts before relying on the level."
        ) if override is True else None
        return status

    if override is False:
        reason = ("agent.reasoning_passthrough is set to false, which disables "
                  "transmission for every route")
    else:
        reason = (
            f"this route is not auto-detected as reasoning-capable "
            f"(provider={provider or 'unknown'}, base_url={base_url or 'unset'}). "
            "Hermes only sends the field to routes known to accept it — direct "
            "Nous Portal, OpenRouter reasoning models, LM Studio, Ollama and "
            "GitHub Models — because other endpoints answer 400. If this "
            "endpoint does accept it, set agent.reasoning_passthrough: true."
        )
    status["summary"] = (
        f"reasoning effort {configured!r} is configured but NOT sent to the provider"
    )
    status["reason"] = reason
    return status


def warning_line(status: Dict[str, Any]) -> Optional[str]:
    """One-line operator warning, or None when nothing is wrong.

    Emitted once per session — a configured-but-inert setting is worth saying
    exactly once, not on every turn.
    """
    if status.get("will_be_sent") or not status.get("configured_effort"):
        return None
    return (
        f"reasoning_effort={status['configured_effort']!r} is configured but will "
        f"NOT be sent: {status.get('reason')}"
    )


__all__ = [
    "passthrough_override",
    "configured_effort",
    "describe",
    "warning_line",
]
