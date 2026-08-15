"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_checkpoint",
})

_DEFAULT_MAX_ATTEMPTS = 2


def _is_dispatcher_kanban_worker() -> bool:
    """Return whether this execution owns a dispatcher-spawned task."""
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return False


def _deadline_warning_fraction() -> float:
    """Read the deadline-warning policy, failing closed on bad config."""
    try:
        from hermes_cli.config import load_config

        kanban = (load_config() or {}).get("kanban") or {}
        fraction = float(kanban.get("deadline_warning_fraction", 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0
    return fraction if 0.0 < fraction <= 1.0 else 0.0


def _safe_checkpoint_enabled() -> bool:
    """Return the checkpoint policy switch, failing closed on config errors."""
    try:
        from hermes_cli.config import load_config

        checkpoint = ((load_config() or {}).get("kanban") or {}).get(
            "safe_checkpoint"
        ) or {}
        from hermes_cli.kanban_config import enabled

        return enabled(checkpoint.get("enabled", False))
    except Exception:
        return False


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def build_kanban_deadline_warning(
    *,
    issued: bool = False,
    now: Optional[float] = None,
    messages: Iterable[dict] | None = None,
) -> Optional[str]:
    """Return a one-shot checkpoint/finalize nudge near a worker deadline.

    The dispatcher supplies an absolute deadline and the run cap at spawn.
    This is deliberately advisory: timeout enforcement remains exclusively in
    the dispatcher.
    """
    if (
        issued
        or session_called_kanban_terminal(messages)
        or not _is_dispatcher_kanban_worker()
    ):
        return None
    fraction = _deadline_warning_fraction()
    if fraction == 0.0:
        return None
    try:
        deadline = float(os.environ["HERMES_KANBAN_RUNTIME_DEADLINE"])
        cap_seconds = float(os.environ["HERMES_KANBAN_RUNTIME_CAP_SECONDS"])
    except (KeyError, TypeError, ValueError):
        return None
    if deadline <= 0.0 or cap_seconds <= 0.0:
        return None

    current_time = time.time() if now is None else now
    warning_at = deadline - cap_seconds * (1.0 - fraction)
    if current_time <= warning_at:
        return None

    percent = f"{fraction * 100:g}%"
    if _safe_checkpoint_enabled():
        action = "Checkpoint now at a coherent boundary, or finish/block."
    else:
        action = (
            "Safe checkpointing is disabled; finish or block at a coherent "
            "boundary."
        )
    return f"[System: You are past {percent} of your runtime cap. {action}]"


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if a terminal Kanban operation durably succeeded this run."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                content = msg.get("content")
                if isinstance(content, str):
                    try:
                        result = json.loads(content)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(result, dict) and result.get("ok") is True:
                        return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_deadline_warning",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
