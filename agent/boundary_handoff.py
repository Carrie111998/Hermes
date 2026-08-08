"""Turn-end guard for incomplete work paused without a boundary handoff.

Issue #80772: a pause at a permission, safety, workspace, repository, or
external-system boundary is legitimate, but ending the turn with only a
partial-progress summary is not. When task-tracking evidence shows remaining
work, a text-only stop (no ``clarify`` this turn) is an invalid terminal
state. The conversation loop nudges once or twice so the model either
finishes and clears todos or calls ``clarify`` with a self-contained handoff.

Policy-only and language-agnostic: this module never parses freeform prose,
never calls tools, and never mutates the todo store. It returns a synthetic
follow-up string or ``None``. Structured evidence only — remaining todos and
whether ``clarify`` already ran — matching ``verification_stop`` /
``kanban_stop``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

_ACTIVE_STATUSES = frozenset({"pending", "in_progress"})
_DEFAULT_MAX_ATTEMPTS = 2
_MAX_REMAINING_IN_NUDGE = 6


def remaining_todo_items(todo_store: Any) -> list[dict[str, str]]:
    """Return pending/in_progress todo items from a TodoStore-like object."""
    if todo_store is None:
        return []
    read = getattr(todo_store, "read", None)
    if not callable(read):
        return []
    try:
        items = read() or []
    except Exception:
        return []
    remaining: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in _ACTIVE_STATUSES:
            remaining.append(
                {
                    "id": str(item.get("id") or "?"),
                    "content": str(item.get("content") or "").strip(),
                    "status": status,
                }
            )
    return remaining


def has_remaining_work(todo_store: Any) -> bool:
    """True when task-tracking evidence shows unfinished work."""
    return bool(remaining_todo_items(todo_store))


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


def _is_synthetic_user_message(msg: dict) -> bool:
    """True for runtime-injected user turns (nudges), not the human user."""
    return any(str(key).endswith("_synthetic") for key in msg)


def turn_called_clarify(messages: Iterable[dict] | None) -> bool:
    """True if this user turn already invoked ``clarify``.

    Scans assistant messages after the last real (non-synthetic) user message
    so a later text reply does not erase an earlier clarify in the same turn.
    """
    if not messages:
        return False
    for msg in reversed(list(messages)):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user" and not _is_synthetic_user_message(msg):
            break
        if role != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if _tool_call_name(tc) == "clarify":
                return True
    return False


def is_valid_terminal_for_remaining_work(
    *,
    todo_store: Any = None,
    messages: Iterable[dict] | None = None,
) -> bool:
    """True when todos are settled, or ``clarify`` already asked this turn."""
    if not has_remaining_work(todo_store):
        return True
    return turn_called_clarify(messages)


def _format_remaining(items: Sequence[dict[str, str]]) -> str:
    lines: list[str] = []
    for item in items[:_MAX_REMAINING_IN_NUDGE]:
        label = item.get("content") or item.get("id") or "?"
        lines.append(f"- [{item.get('status', '?')}] {label}")
    omitted = len(items) - len(lines)
    if omitted > 0:
        lines.append(f"- ... and {omitted} more")
    return "\n".join(lines)


def build_boundary_handoff_nudge(
    *,
    todo_store: Any = None,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    enabled: bool = True,
    clarify_available: bool = True,
) -> Optional[str]:
    """Return a synthetic follow-up when remaining work ends without ``clarify``.

    Returns ``None`` when the guard should not fire (disabled, ``clarify`` not
    in the toolset, no remaining todos, clarify already asked, or nudge budget
    exhausted). Freeform reply text is ignored — terminal validity is
    structured evidence only.
    """
    if not enabled or not clarify_available or attempts >= max_attempts:
        return None

    remaining = remaining_todo_items(todo_store)
    if not remaining:
        return None

    if turn_called_clarify(messages):
        return None

    return (
        "[System: Task-tracking still shows unfinished work, but you stopped "
        "without calling `clarify`.\n\n"
        "Outstanding items:\n"
        f"{_format_remaining(remaining)}\n\n"
        "A plain progress summary is not a valid terminal state here. Either:\n"
        "1. Finish the remaining work now and mark those todos completed, OR\n"
        "2. If you must pause at a permission, safety, workspace, repository, "
        "or external-system boundary, call `clarify` with a self-contained "
        "question that states (a) what you completed and verified, (b) what "
        "remains, (c) the exact boundary or blocker, (d) whether any live or "
        "external state changed, and (e) the decision or authorization needed "
        "to continue.\n\n"
        "Do not end with ambiguity about whether the task is complete, paused, "
        "blocked, or abandoned.]"
    )


__all__ = [
    "build_boundary_handoff_nudge",
    "has_remaining_work",
    "is_valid_terminal_for_remaining_work",
    "remaining_todo_items",
    "turn_called_clarify",
]
