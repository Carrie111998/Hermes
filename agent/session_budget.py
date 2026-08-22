"""Per-session cumulative token budget guard (issue #91713).

Existing runaway-cost controls are context- or wall-clock-based:
``compression.threshold_tokens`` caps a *single* request's context, and the
run-budget/watchdog bounds *elapsed time*. Neither catches a session making
many rapid small calls that never trip the compression threshold — each call
is cheap, the aggregate is not.

This module adds a cumulative cap counted across *all* API calls of a session
(input + output), including auxiliary forks (background review, MoA). It is a
pure-local guard over counters the agent loop already maintains
(``session_total_tokens``) plus an aux accumulator
(``session_aux_tokens_for_budget``) fed at the aux-recording chokepoints.

Default off: ``session_budget_tokens`` of ``None``/``0`` means unlimited, so
the default path has zero behavior change — mirroring ``run_budget_seconds``.
"""

from typing import Any, Optional

__all__ = [
    "normalize_budget_tokens",
    "normalize_budget_action",
    "budget_used_tokens",
    "budget_remaining_tokens",
    "budget_exceeded",
    "evaluate_breach",
    "budget_exhausted_message",
]


def normalize_budget_tokens(value: Any) -> Optional[int]:
    """Normalize a cumulative token budget to a positive int or None.

    None / absent / non-numeric / zero / negative all resolve to ``None``
    (feature off = unlimited), so a malformed config value can never activate
    the guard, only leave it dormant. ``bool`` is rejected because YAML
    ``true`` would otherwise become a 1-token budget.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens > 0 else None


def normalize_budget_action(value: Any) -> str:
    """Normalize the breach action to ``"abort"`` (default) or ``"warn"``."""
    if isinstance(value, str) and value.strip().lower() == "warn":
        return "warn"
    return "abort"


def budget_used_tokens(agent: Any) -> int:
    """Cumulative tokens counted against the budget for this session.

    Sums the main-loop total (``session_total_tokens`` — already includes the
    MoA/codex path) and the auxiliary accumulator
    (``session_aux_tokens_for_budget`` — background-review forks), so aux spend
    counts toward the cap even though it is not part of ``session_total_tokens``.
    """
    return int(getattr(agent, "session_total_tokens", 0) or 0) + int(
        getattr(agent, "session_aux_tokens_for_budget", 0) or 0
    )


def budget_remaining_tokens(agent: Any) -> Optional[int]:
    """Tokens left before the cap, or ``None`` when no budget is set."""
    cap = normalize_budget_tokens(getattr(agent, "session_budget_tokens", None))
    if cap is None:
        return None
    return max(0, cap - budget_used_tokens(agent))


def budget_exceeded(agent: Any) -> bool:
    """True when a budget is set and cumulative usage has reached the cap."""
    cap = normalize_budget_tokens(getattr(agent, "session_budget_tokens", None))
    if cap is None:
        return False
    return budget_used_tokens(agent) >= cap


def evaluate_breach(agent: Any) -> Optional[str]:
    """Decide the guard action for the current budget state.

    Returns ``"abort"`` on every breach when the action is ``abort``; returns
    ``"warn"`` on the *first* breach in ``warn`` mode and ``None`` on every
    later warn-mode breach (so a session over budget warns once and keeps
    going); returns ``None`` when no budget is set or the cap is not yet
    reached. The one-shot warn state latches on ``agent._session_budget_warned``
    (reset with the session counters). Isolating the decision here keeps the
    abort/warn/warn-once behavior unit-testable without the full turn loop.
    """
    if not budget_exceeded(agent):
        return None
    if normalize_budget_action(getattr(agent, "session_budget_action", "abort")) == "abort":
        return "abort"
    if not getattr(agent, "_session_budget_warned", False):
        agent._session_budget_warned = True
        return "warn"
    return None


def budget_exhausted_message(agent: Any) -> str:
    """User-facing message for an exhausted session token budget."""
    calls = int(getattr(agent, "session_api_calls", 0) or 0)
    cap = normalize_budget_tokens(getattr(agent, "session_budget_tokens", None)) or 0
    used = budget_used_tokens(agent)
    return (
        f"⚠️  session token budget exhausted after {calls} calls "
        f"({used:,}/{cap:,} tokens). Further turns are refused until "
        f"agent.session_budget_tokens is raised or cleared in config "
        f"(applies on the next session)."
    )
