"""Bounded session-scoped mutation authority for protected turns."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional


class SessionWritePolicyMode(str, Enum):
    NORMAL = "NORMAL"
    DENY_ALL = "DENY_ALL"


@dataclass(frozen=True)
class SessionWritePolicy:
    session_id: str = ""
    mode: SessionWritePolicyMode = SessionWritePolicyMode.NORMAL
    origin: str = "default"
    protected: bool = False

    @classmethod
    def normal(cls, session_id: str = "", *, origin: str = "default") -> "SessionWritePolicy":
        return cls(session_id=session_id, origin=origin)

    @classmethod
    def deny_all(cls, session_id: str = "", *, origin: str = "protected") -> "SessionWritePolicy":
        return cls(
            session_id=session_id,
            mode=SessionWritePolicyMode.DENY_ALL,
            origin=origin,
            protected=True,
        )

    @property
    def denies_mutations(self) -> bool:
        return self.mode is SessionWritePolicyMode.DENY_ALL


_CURRENT_POLICY: ContextVar[Optional[SessionWritePolicy]] = ContextVar(
    "hermes_session_write_policy", default=None
)


def require_turn_policy(
    retained: object,
    *,
    protected: bool,
    session_id: str = "",
) -> Optional[SessionWritePolicy]:
    """Return a bindable retained policy, failing closed for protected turns."""
    if type(retained) is SessionWritePolicy:
        mode = retained.mode
        valid = (
            isinstance(mode, SessionWritePolicyMode)
            and isinstance(retained.session_id, str)
            and isinstance(retained.protected, bool)
            and retained.protected is protected
        )
        if protected:
            valid = valid and bool(session_id) and retained.session_id == session_id
        if valid:
            return retained
    if protected:
        return None
    return SessionWritePolicy.normal(session_id=session_id, origin="implicit_normal")


def get_current_session_write_policy() -> SessionWritePolicy:
    policy = _CURRENT_POLICY.get()
    return policy or SessionWritePolicy.normal(origin="unbound_normal")


@contextmanager
def session_write_policy_scope(policy: SessionWritePolicy) -> Iterator[SessionWritePolicy]:
    if not isinstance(policy, SessionWritePolicy):
        raise TypeError("session write policy must be a SessionWritePolicy")
    token: Token = _CURRENT_POLICY.set(policy)
    try:
        yield policy
    finally:
        _CURRENT_POLICY.reset(token)


__all__ = [
    "SessionWritePolicy",
    "SessionWritePolicyMode",
    "get_current_session_write_policy",
    "require_turn_policy",
    "session_write_policy_scope",
]
