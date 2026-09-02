"""Config-driven per-model execution budgets.

The execution middleware calls :func:`decide_tool_call` immediately before a
tool is dispatched. Budgets are opt-in through
``agent.model_execution_budgets`` in ``config.yaml``; an absent or invalid
configuration leaves execution unchanged. The state is kept on the active
agent and scoped to its current turn, so this module does not change prompts,
tool schemas, or conversation messages.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# These are the public delegation entry points.  Delegation is a handoff rather
# than execution by the current model, so it never consumes this budget.
DELEGATION_TOOL_NAMES = frozenset({"delegate", "delegate_task"})

_STATE_INIT_LOCK = threading.Lock()
_FALLBACK_LOCK = threading.RLock()
_FALLBACK_STATE = None


@dataclass
class _BudgetState:
    """Mutable budget window stored on an agent instance."""

    window_key: str = ""
    limit: int | None = None
    used: int = 0


def _normalise_model(value: Any) -> str:
    return str(value or "").strip().casefold()


def _model_candidates(model: Any) -> tuple[str, ...]:
    """Return normalized full and provider-free model names."""
    normalized = _normalise_model(model)
    if not normalized:
        return ("",)
    candidates = [normalized]
    if "/" in normalized:
        provider_free = normalized.rsplit("/", 1)[-1]
        if provider_free and provider_free != normalized:
            candidates.append(provider_free)
    return tuple(candidates)


def _coerce_limit(value: Any) -> int | None:
    """Return a non-negative integer limit, or ``None`` for invalid values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None
    return None


def _pattern_specificity(pattern: str, position: int) -> tuple[int, int, int]:
    """Rank a matching pattern; ties retain config insertion order."""
    wildcard_count = sum(pattern.count(char) for char in "*?[")
    literal_count = len(pattern) - wildcard_count
    return literal_count, -wildcard_count, -position


def _pattern_matches(pattern: str, model: Any) -> bool:
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for candidate in _model_candidates(model)
    )


def execution_budget_for_model(
    model: Any,
    config: Mapping[str, Any] | None = None,
) -> int | None:
    """Resolve the configured budget for *model*.

    Patterns use shell-style wildcards (``*``, ``?`` and character classes)
    and are matched case-insensitively against both the full model identifier
    and the portion after its final ``/``.  When patterns overlap, the most
    specific match wins; ties use the order from ``config.yaml``.

    ``None`` means that no budget is configured.  Malformed configuration is
    deliberately treated as disabled so a typo cannot change the default
    execution behavior or prevent an agent from starting.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            logger.debug("Unable to load execution budget configuration", exc_info=True)
            return None

    if not isinstance(config, Mapping):
        return None
    agent_config = config.get("agent")
    if not isinstance(agent_config, Mapping):
        return None
    raw_budgets = agent_config.get("model_execution_budgets")
    if not isinstance(raw_budgets, Mapping) or not raw_budgets:
        return None

    normalized_model = _normalise_model(model)
    best: tuple[tuple[int, int, int], int] | None = None
    for position, (raw_pattern, raw_limit) in enumerate(raw_budgets.items()):
        if not isinstance(raw_pattern, str):
            continue
        pattern = raw_pattern.strip().casefold()
        if not pattern or not _pattern_matches(pattern, normalized_model):
            continue
        limit = _coerce_limit(raw_limit)
        if limit is None:
            # Ignore only this malformed entry; a valid fallback pattern may
            # still apply to the same model.
            continue
        score = _pattern_specificity(pattern, position)
        if best is None or score > best[0]:
            best = (score, limit)
    return best[1] if best is not None else None


# Short alias for callers that prefer the noun-first spelling.
budget_for_model = execution_budget_for_model


def _turn_key(agent: Any) -> str:
    turn_id = str(getattr(agent, "_current_turn_id", "") or "").strip()
    if turn_id:
        return turn_id
    task_id = str(getattr(agent, "_current_task_id", "") or "").strip()
    if task_id:
        return task_id
    # Agent instances own their state, so this sentinel scopes an agent that
    # does not expose turn metadata without introducing process-global state.
    return "__agent_lifetime__"


def _agent_lock(agent: Any) -> threading.RLock:
    lock = getattr(agent, "_model_execution_budget_lock", None)
    if lock is not None:
        return lock
    with _STATE_INIT_LOCK:
        lock = getattr(agent, "_model_execution_budget_lock", None)
        if lock is None:
            lock = threading.RLock()
            try:
                setattr(agent, "_model_execution_budget_lock", lock)
            except Exception:
                return _FALLBACK_LOCK
    return lock


def _agent_state(agent: Any) -> _BudgetState:
    state = getattr(agent, "_model_execution_budget_state", None)
    if state is not None:
        return state
    with _STATE_INIT_LOCK:
        state = getattr(agent, "_model_execution_budget_state", None)
        if state is None:
            state = _BudgetState()
            try:
                setattr(agent, "_model_execution_budget_state", state)
            except Exception:
                # AIAgent is mutable.  This fallback only protects unusual
                # agent-like objects used by integrations and tests.
                global _FALLBACK_STATE
                if _FALLBACK_STATE is None:
                    _FALLBACK_STATE = state
                return _FALLBACK_STATE
    return state


def _refresh_window(agent: Any, state: _BudgetState) -> None:
    key = _turn_key(agent)
    if key == state.window_key:
        return
    state.window_key = key
    state.used = 0
    state.limit = execution_budget_for_model(getattr(agent, "model", ""))


def is_delegation_tool(function_name: Any) -> bool:
    return str(function_name or "").strip().casefold() in DELEGATION_TOOL_NAMES


def decide_tool_call(
    agent: Any,
    *,
    function_name: str,
    final_args: Mapping[str, Any] | None = None,
    tool_call_id: str = "",
) -> dict[str, Any]:
    """Return an allow/block decision for one imminent tool dispatch.

    ``final_args`` and ``tool_call_id`` are accepted to keep the API aligned
    with the execution middleware.  Budget decisions use only the tool name;
    arguments are never persisted or inspected.
    """
    del final_args, tool_call_id
    lock = _agent_lock(agent)
    with lock:
        state = _agent_state(agent)
        _refresh_window(agent, state)

        if state.limit is None:
            return {
                "action": "allow",
                "reason": "disabled",
                "count": 0,
                "limit": None,
            }

        if is_delegation_tool(function_name):
            return {
                "action": "allow",
                "reason": "delegation",
                "count": state.used,
                "limit": state.limit,
            }

        if state.used >= state.limit:
            return {
                "action": "block",
                "reason": "execution_budget_exceeded",
                "count": state.used,
                "limit": state.limit,
            }

        state.used += 1
        return {
            "action": "allow",
            "reason": "within_budget",
            "count": state.used,
            "limit": state.limit,
        }


def execution_call_count(agent: Any) -> int:
    """Return the number of allowed non-delegation calls in the current window."""
    with _agent_lock(agent):
        state = _agent_state(agent)
        _refresh_window(agent, state)
        return state.used


# Compatibility-friendly name for tests and integrations.
worker_call_count = execution_call_count


def reset_budget(agent: Any) -> None:
    """Reset the current agent's budget window."""
    with _agent_lock(agent):
        state = _agent_state(agent)
        state.window_key = ""
        state.limit = None
        state.used = 0
