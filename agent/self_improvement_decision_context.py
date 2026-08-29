"""Typed ContextVar authority for self-improvement writes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional


def _deny_fallback() -> object:
    try:
        from agent.self_improvement_policy import DENY_UNKNOWN_OPERATION, Decision
        return Decision(DENY_UNKNOWN_OPERATION, "missing self-improvement decision; fail-closed")
    except Exception:
        class FallbackDecision:
            result = "DENY"
            reason = "missing self-improvement decision; fail-closed"
            @property
            def allow(self) -> bool:
                return False
        return FallbackDecision()


DENY_FALLBACK_DECISION = _deny_fallback()
hermes_self_improvement_decision: ContextVar[Optional[object]] = ContextVar(
    "hermes_self_improvement_decision", default=None
)


def _is_decision_instance(value: object) -> bool:
    try:
        from agent.self_improvement_policy import Decision
    except Exception:
        return False
    return type(value) is Decision


def require_retained_self_improvement_decision(retained: object) -> object:
    """Return retained authority or the explicit deny fallback.

    A malformed or stale object is not normalized into authority. Conversation
    dispatch may continue, but self-improvement writes receive a concrete deny.
    """
    return retained if _is_decision_instance(retained) else DENY_FALLBACK_DECISION


def get_self_improvement_decision() -> object:
    try:
        decision = hermes_self_improvement_decision.get()
    except Exception:
        return DENY_FALLBACK_DECISION
    return require_retained_self_improvement_decision(decision)


def bind_self_improvement_decision(decision: object) -> Token:
    if not _is_decision_instance(decision):
        raise ValueError(f"expected Decision-like instance, got {type(decision).__name__}")
    return hermes_self_improvement_decision.set(decision)


def reset_self_improvement_decision(token: Token) -> None:
    hermes_self_improvement_decision.reset(token)


@contextmanager
def self_improvement_decision_scope(decision: object) -> Iterator[object]:
    token = bind_self_improvement_decision(decision)
    try:
        yield decision
    finally:
        hermes_self_improvement_decision.reset(token)


__all__ = [
    "DENY_FALLBACK_DECISION", "bind_self_improvement_decision",
    "get_self_improvement_decision", "reset_self_improvement_decision",
    "require_retained_self_improvement_decision", "self_improvement_decision_scope",
]
