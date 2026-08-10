"""Invocation-scoped approval policy for one-shot CLI runs."""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass


_ONESHOT_YOLO_POLICY: ContextVar[bool | None] = ContextVar(
    "_ONESHOT_YOLO_POLICY", default=None
)


@dataclass(frozen=True)
class OneShotPolicyToken:
    context_token: Token
    inherited_yolo: str | None


def current_oneshot_yolo_policy() -> bool | None:
    """Return exact one-shot policy, or ``None`` outside one-shot mode."""
    return _ONESHOT_YOLO_POLICY.get()


def configure_oneshot_approval_policy() -> OneShotPolicyToken | None:
    """Resolve and install the fail-closed policy before tool discovery.

    Only the literal YAML boolean ``true`` enables one-shot auto-approval.
    Missing, false, malformed, and unreadable configuration all deny it.
    """
    if current_oneshot_yolo_policy() is not None:
        return None

    enabled = False
    try:
        from hermes_cli.config import read_user_config_raw

        approvals = (read_user_config_raw() or {}).get("approvals")
        if isinstance(approvals, dict):
            enabled = approvals.get("oneshot_yolo") is True
    except Exception:
        enabled = False

    inherited_yolo = os.environ.get("HERMES_YOLO_MODE")
    if enabled:
        os.environ["HERMES_YOLO_MODE"] = "1"
    else:
        os.environ.pop("HERMES_YOLO_MODE", None)

    return OneShotPolicyToken(
        context_token=_ONESHOT_YOLO_POLICY.set(enabled),
        inherited_yolo=inherited_yolo,
    )


def reset_oneshot_approval_policy(token: OneShotPolicyToken) -> None:
    """Restore policy and inherited YOLO after an embedded invocation."""
    _ONESHOT_YOLO_POLICY.reset(token.context_token)
    if token.inherited_yolo is None:
        os.environ.pop("HERMES_YOLO_MODE", None)
    else:
        os.environ["HERMES_YOLO_MODE"] = token.inherited_yolo
