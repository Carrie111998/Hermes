"""Configuration helpers for the bounded ``pre_response`` continuation gate."""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_MAX_RESPONSE_CONTINUATIONS = 1


def max_response_continuations(
    config: Optional[dict[str, Any]] = None,
) -> int:
    """Return the per-turn continuation bound for ``pre_response`` (>= 0)."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}

    agent_cfg = (config or {}).get("agent") if isinstance(config, dict) else None
    raw = (
        agent_cfg.get("max_response_continuations")
        if isinstance(agent_cfg, dict)
        else None
    )
    if raw is None:
        return DEFAULT_MAX_RESPONSE_CONTINUATIONS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESPONSE_CONTINUATIONS


__all__ = [
    "DEFAULT_MAX_RESPONSE_CONTINUATIONS",
    "max_response_continuations",
]
