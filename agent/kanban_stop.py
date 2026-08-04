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

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2

# When the nudge budget is exhausted and the worker STILL exits with a plain
# narration (finish_reason=stop, no terminal board tool), we must not let the
# process return rc=0 and leave the card silently `running` — that is exactly
# the protocol_violation class this module exists to prevent. Instead, build a
# concrete `kanban_block` payload the harness can fire so the card lands in a
# visible, routable `blocked` state with a real reason (never silently
# `running`, never a phantom `complete`). See t_44cfa735.
_BLOCK_REASON_PREFIX = "auto-block: worker exited without kanban_complete/kanban_block"


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
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
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


def build_kanban_stop_fallback_block(
    *,
    final_response: Optional[str] = None,
    model: Optional[str] = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[dict]:
    """Build a concrete ``kanban_block`` payload when the nudge budget is spent.

    The agent turn-end guard (``build_kanban_stop_nudge``) gives the model up
    to ``max_attempts`` chances to emit ``kanban_complete`` / ``kanban_block``.
    If the worker STILL ends the turn with a plain narration and no terminal
    board tool, returning a synthetic nudge again would just loop forever —
    and letting the process exit rc=0 leaves the card silently ``running``,
    which the dispatcher records as a ``protocol_violation`` (the dominant
    worker-failure mode tracked in t_44cfa735).

    This returns a ready-to-fire ``kanban_block(reason=...)`` payload so the
    harness can terminate the card in a visible, routable ``blocked`` state
    with a real reason recorded — never silently ``running``, never a phantom
    ``complete``. The reason is prefixed so dashboards/analyzers can attribute
    the block to the protocol-violation auto-guard rather than a real human
    gate.

    Returns ``None`` when the guard should not fire (not a kanban worker, the
    session already called a terminal tool, or the nudge budget is not yet
    exhausted — in which case the caller should still issue one more nudge).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts < max_attempts:
        # Nudge budget not exhausted: caller should still try to coax a
        # terminal call out of the model before falling back to a hard block.
        return None
    if session_called_kanban_terminal(None):
        # Defensive: if a terminal tool ran at any point this session, do not
        # double-block. (session_called_kanban_terminal scans live messages.)
        return None
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    snippet = (final_response or "").strip().replace("\n", " ")[:280]
    reason_parts = [
        f"{_BLOCK_REASON_PREFIX} after {attempts} nudge attempt(s).",
        f"The worker ended its turn with only narration and no terminal board "
        f"call (model={model or 'unknown'}).",
    ]
    if snippet:
        reason_parts.append(f"Final narration (truncated): \"{snippet}\"")
    reason_parts.append(
        "Route: a human must verify whether the work was actually completed "
        "(check artifacts/comments) and either unblock for retry or complete it."
    )
    return {
        "reason": " ".join(reason_parts),
        # Mark the kind so routing/analytics can separate this auto-guard block
        # from a genuine human/dependency gate. 'capability' is the closest
        # valid kind: the worker could not perform the required terminal
        # transition on its own.
        "kind": "capability",
        "auto_guard": True,
        "task_id": tid,
        "nudge_attempts": attempts,
    }


__all__ = [
    "build_kanban_stop_nudge",
    "build_kanban_stop_fallback_block",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
