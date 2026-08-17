"""Shared agent iteration-budget resolution for all entry points."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


DEFAULT_AGENT_MAX_TURNS = 500


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_agent_max_turns(
    config: Mapping[str, Any] | None,
    *,
    explicit: int | None = None,
    environ: Mapping[str, str] | None = None,
    default: int = DEFAULT_AGENT_MAX_TURNS,
) -> int:
    """Resolve max turns with CLI-compatible precedence.

    Precedence is explicit override, ``agent.max_turns``, the legacy root
    ``max_turns`` key, ``HERMES_MAX_ITERATIONS``, then the default.
    """
    config = config or {}
    agent_config = config.get("agent")
    if not isinstance(agent_config, Mapping):
        agent_config = {}
    environment = os.environ if environ is None else environ

    for value in (
        explicit,
        agent_config.get("max_turns"),
        config.get("max_turns"),
        environment.get("HERMES_MAX_ITERATIONS"),
        default,
    ):
        resolved = _positive_int(value)
        if resolved is not None:
            return resolved
    return DEFAULT_AGENT_MAX_TURNS
