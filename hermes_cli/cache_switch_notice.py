"""Cache-rebuild notice for mid-session model switches.

Switching models mid-conversation invalidates the prompt cache the new
model would otherwise hit: providers key prompt caches per model, so the
first call after a switch re-reads the entire context fully uncached —
every tool result, file read, and web page in the session, billed at full
input price once. On a short session that costs pennies; on a long one it
is real money, and users have no way to see it coming.

This module builds a single informational line appended to the ``/model``
switch confirmation on every surface (CLI, TUI, gateway). It is NOT a
confirmation gate — the switch already happened; the notice only makes the
one-time cost visible and offers the session-scoped revert command.

Design constraints honored here (see AGENTS.md):

- Informational only. Never blocks, never prompts, never mutates context —
  the note is display output, so prompt caching and message alternation are
  untouched.
- Fires only when it matters: below ``MIN_CONTEXT_TOKENS`` the re-read cost
  is negligible and the line would be noise, so nothing is emitted. Empty
  and near-empty sessions stay silent.
- Gated by ``display.cache_switch_notice`` in config.yaml (never env vars).
"""

from __future__ import annotations

from typing import Any, List, Optional

# Below this estimated context size, the uncached re-read after a switch is
# cheap enough that warning about it is pure noise. ~30k tokens ≈ several
# substantial exchanges (or one long document read).
MIN_CONTEXT_TOKENS = 30_000


def cache_switch_notice_enabled() -> bool:
    """Read ``display.cache_switch_notice`` (default: enabled).

    Config failures degrade to "enabled" — a broken config.yaml should not
    silently suppress a cost signal.
    """
    try:
        from hermes_cli.config import load_config_readonly

        display = (load_config_readonly() or {}).get("display") or {}
        value = display.get("cache_switch_notice")
        if value is None:
            return True
        return bool(value)
    except Exception:
        return True


def _compressor_reported_tokens(agent: Any) -> int:
    """Provider-reported prompt tokens from the agent's context engine.

    Returns 0 when unknown. ``last_prompt_tokens`` parks at a -1 sentinel
    right after a compression (awaiting real usage) — clamp it to 0, the
    same treatment the status bar applies (cli.py snapshot path).
    """
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return 0
    try:
        tokens = int(getattr(compressor, "last_prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return tokens if tokens > 0 else 0


def _rough_estimate_tokens(agent: Any) -> int:
    """Rough token estimate over the live conversation as a fallback.

    Used when no provider-reported count exists yet (fresh session, or the
    first turn before any usage came back). Mirrors the payload buckets
    Hermes actually sends: system prompt, messages, tool schemas.
    """
    from agent.model_metadata import estimate_request_tokens_rough

    messages: List[dict] = [
        m
        for m in (getattr(agent, "conversation_history", None) or [])
        if isinstance(m, dict)
    ]
    system_prompt = str(getattr(agent, "system_prompt", "") or "")
    tools = getattr(agent, "tools", None)
    try:
        return int(
            estimate_request_tokens_rough(messages, system_prompt=system_prompt, tools=tools)
        )
    except Exception:
        return 0


def estimate_context_tokens(agent: Any) -> int:
    """Best-effort context size for the cache-notice decision.

    Prefers the provider-reported prompt token count from the last real API
    call; falls back to a rough structural estimate. Returns 0 when neither
    is available (bare/test agents) — callers treat 0 as "below threshold".
    """
    if agent is None:
        return 0
    reported = _compressor_reported_tokens(agent)
    if reported > 0:
        return reported
    return _rough_estimate_tokens(agent)


def build_cache_switch_notice(
    *,
    old_model_display: str,
    new_model_display: str,
    est_context_tokens: int,
) -> Optional[str]:
    """Build the user-facing notice, or None when it should stay silent.

    Silent when:
    - the "switch" didn't change the model (same-model re-select keeps the
      cache warm — there is nothing to warn about),
    - the estimated context is below :data:`MIN_CONTEXT_TOKENS`,
    - the config toggle is off.
    """
    if not old_model_display or not new_model_display:
        return None
    if old_model_display == new_model_display:
        return None
    if est_context_tokens < MIN_CONTEXT_TOKENS:
        return None

    from agent.i18n import t

    # Round to nearest thousand with half-up (not banker's), so 30_500 → 31k.
    k = max(1, int((est_context_tokens + 500) // 1000))
    return "\n".join(
        [
            t(
                "gateway.model.cache_switch_notice",
                model=new_model_display,
                tokens=f"{k}k",
            ),
            t("gateway.model.cache_switch_revert_hint", model=old_model_display),
        ]
    )


def cache_switch_notice_for_agent(
    *,
    agent: Any,
    old_model_display: str,
    new_model_display: str,
) -> Optional[str]:
    """Convenience wrapper: gate on config, estimate, and build in one call.

    All three surfaces (CLI apply path, gateway typed-command path, gateway
    picker callback) call this with their live agent reference.
    """
    if not cache_switch_notice_enabled():
        return None
    return build_cache_switch_notice(
        old_model_display=old_model_display,
        new_model_display=new_model_display,
        est_context_tokens=estimate_context_tokens(agent),
    )
