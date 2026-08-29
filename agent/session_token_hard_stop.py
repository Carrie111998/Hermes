"""Per-session billed-token hard stop (``agent.session_token_hard_stop``).

Operators can set this key believing it is a fuse. Until #96814 it was
accepted (or silently ignored) with no reader. This module is the reader.

Formula (documented, billed tokens — not a turn cap):

    used = session_input_tokens
         + session_cache_read_tokens
         + session_cache_write_tokens
         + session_output_tokens

That matches :attr:`agent.usage_pricing.CanonicalUsage.total_tokens` as
accumulated on ``session_*`` counters. Fresh input + output alone would miss
the cache-read-dominated runaway the issue describes; cache tokens are billed
and are therefore part of the fuse.

``0`` / ``null`` / absent / invalid = disabled (default). The check is
evaluated at the top of the conversation loop, before the next provider
call, so crossing the cap does not spend another request.
"""

from __future__ import annotations

from typing import Any, Optional

# Older names that were accepted (or written by operators) with no
# implementation. Warn at config load — silently accepting a safety key
# is worse than rejecting it.
UNIMPLEMENTED_BUDGET_KEYS = frozenset(
    {
        "gateway_usage_hard_api_calls",
        "gateway_usage_hard_tokens",
        "gateway_usage_warn_api_calls",
        "gateway_usage_warn_tokens",
        "gateway_usage_hard_api_calls_warn",
        "gateway_usage_hard_tokens_warn",
    }
)

SESSION_TOKEN_WARN_NOTICE = (
    "[SYSTEM NOTICE — session token budget] "
    "This session is approaching its billed-token ceiling. "
    "Wind down: stop new discovery, finish the current deliverable, "
    "and avoid starting work that needs another long tool loop."
)


def normalize_session_token_limit(value: Any) -> Optional[int]:
    """Normalize a token-limit config value to a positive int or None.

    None / absent / non-numeric / non-positive / bool all resolve to
    ``None`` (feature off) so a malformed YAML value can never arm the
    fuse, only leave it dormant.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return tokens


def billed_session_tokens(agent: Any) -> int:
    """Return billed tokens accumulated on this session.

    Prefers the split canonical counters (fresh + cache + output). Falls
    back to ``session_total_tokens`` when the split counters are absent
    (older pickles / minimal stubs).
    """
    split_names = (
        "session_input_tokens",
        "session_cache_read_tokens",
        "session_cache_write_tokens",
        "session_output_tokens",
    )
    if any(hasattr(agent, name) for name in split_names):
        try:
            fresh = int(getattr(agent, "session_input_tokens", 0) or 0)
            cache_read = int(getattr(agent, "session_cache_read_tokens", 0) or 0)
            cache_write = int(getattr(agent, "session_cache_write_tokens", 0) or 0)
            output = int(getattr(agent, "session_output_tokens", 0) or 0)
            return max(0, fresh + cache_read + cache_write + output)
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(getattr(agent, "session_total_tokens", 0) or 0))
    except (TypeError, ValueError):
        return 0


def resolve_hard_stop(agent: Any) -> Optional[int]:
    return normalize_session_token_limit(
        getattr(agent, "session_token_hard_stop", None)
    )


def resolve_warn_threshold(agent: Any) -> Optional[int]:
    """Absolute warn threshold in billed tokens.

    Explicit ``agent.session_token_warn`` wins when set. Otherwise, if a
    hard stop is configured, warn at 80% of that ceiling (same shape as
    ``run_budget_seconds``).
    """
    explicit = normalize_session_token_limit(
        getattr(agent, "session_token_warn", None)
    )
    if explicit is not None:
        return explicit
    cap = resolve_hard_stop(agent)
    if cap is None:
        return None
    return max(1, int(cap * 0.8))


def hard_stop_exhausted(agent: Any) -> bool:
    cap = resolve_hard_stop(agent)
    if cap is None:
        return False
    return billed_session_tokens(agent) >= cap


def should_emit_warn(agent: Any) -> bool:
    if getattr(agent, "_session_token_warn_injected", False):
        return False
    threshold = resolve_warn_threshold(agent)
    if threshold is None:
        return False
    if hard_stop_exhausted(agent):
        # Hard stop owns the turn; do not also inject a wind-down notice.
        return False
    return billed_session_tokens(agent) >= threshold


def hard_stop_user_message(agent: Any) -> str:
    cap = resolve_hard_stop(agent) or 0
    used = billed_session_tokens(agent)
    return (
        f"Session token hard stop reached ({used:,}/{cap:,} billed tokens). "
        "Start a new session (/new) to continue."
    )


def unimplemented_budget_keys_present(config: Any) -> list[str]:
    """Return unimplemented budget key names found in a config dict."""
    if not isinstance(config, dict):
        return []
    found: list[str] = []
    agent_section = config.get("agent")
    sections = [config]
    if isinstance(agent_section, dict):
        sections.append(agent_section)
    seen: set[str] = set()
    for section in sections:
        for key in UNIMPLEMENTED_BUDGET_KEYS:
            if key in section and key not in seen:
                seen.add(key)
                found.append(key)
    return sorted(found)
