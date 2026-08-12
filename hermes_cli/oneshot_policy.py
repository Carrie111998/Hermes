"""Invocation-scoped approval policy for one-shot CLI runs."""

from __future__ import annotations

import os
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass


_ONESHOT_YOLO_POLICY: ContextVar[bool | None] = ContextVar(
    "_ONESHOT_YOLO_POLICY", default=None
)
# A process-level deny sentinel covers approval checks reached from plain
# threads that did not inherit the invocation context.  The allow capability
# is deliberately context-local: an unrelated or outliving worker must never
# inherit an exact-true opt-in merely because one-shot is active elsewhere.
_PROCESS_ONESHOT_YOLO_POLICY: bool | None = None
_POLICY_LOCK = threading.RLock()


@dataclass(frozen=True)
class OneShotPolicyToken:
    context_token: Token
    inherited_yolo: str | None
    inherited_process_policy: bool | None


def current_oneshot_yolo_policy() -> bool | None:
    """Return the invocation policy, denying unwrapped one-shot workers.

    Context propagation carries both allow and deny decisions to authorized
    work.  A plain worker sees only the process-level deny sentinel, so losing
    the invocation context cannot either fail open or leak an allow capability.
    """
    context_policy = _ONESHOT_YOLO_POLICY.get()
    if context_policy is not None:
        return context_policy
    with _POLICY_LOCK:
        return _PROCESS_ONESHOT_YOLO_POLICY


def configure_oneshot_approval_policy() -> OneShotPolicyToken | None:
    """Resolve and install the fail-closed policy before tool discovery.

    Only the literal YAML boolean ``true`` enables one-shot auto-approval.
    Missing, false, malformed, and unreadable configuration all deny it.
    """
    global _PROCESS_ONESHOT_YOLO_POLICY

    with _POLICY_LOCK:
        if current_oneshot_yolo_policy() is not None:
            return None

        enabled = False
        ignore_user_config = (
            os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1"
            or os.environ.get("HERMES_SAFE_MODE") == "1"
        )
        if not ignore_user_config:
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

        inherited_process_policy = _PROCESS_ONESHOT_YOLO_POLICY
        # Never publish the allow capability process-wide. Context-aware work
        # sees ``enabled`` through the ContextVar; unwrapped workers fail closed.
        _PROCESS_ONESHOT_YOLO_POLICY = False
        return OneShotPolicyToken(
            context_token=_ONESHOT_YOLO_POLICY.set(enabled),
            inherited_yolo=inherited_yolo,
            inherited_process_policy=inherited_process_policy,
        )


def reset_oneshot_approval_policy(token: OneShotPolicyToken) -> None:
    """Restore policy and inherited YOLO after an embedded invocation."""
    global _PROCESS_ONESHOT_YOLO_POLICY

    with _POLICY_LOCK:
        _ONESHOT_YOLO_POLICY.reset(token.context_token)
        _PROCESS_ONESHOT_YOLO_POLICY = token.inherited_process_policy
        if token.inherited_yolo is None:
            os.environ.pop("HERMES_YOLO_MODE", None)
        else:
            os.environ["HERMES_YOLO_MODE"] = token.inherited_yolo
