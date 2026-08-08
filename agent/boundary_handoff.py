"""Turn-end guard for incomplete work paused without a boundary handoff.

Issue #80772: a pause at a permission, safety, workspace, repository, or
external-system boundary is legitimate, but ending the turn with only a
partial-progress summary is not. When task-tracking evidence shows remaining
work, a final response that lacks both a clear completion claim and a clear
blocker/clarification is an invalid terminal state. The conversation loop
nudges once or twice so the model emits a self-contained handoff (or asks
for the decision) before stopping.

Policy-only: this module never calls tools or mutates the todo store. It
returns a synthetic follow-up string or ``None``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Sequence

_ACTIVE_STATUSES = frozenset({"pending", "in_progress"})
_DEFAULT_MAX_ATTEMPTS = 2
_MAX_REMAINING_IN_NUDGE = 6

# A completion claim is an explicit "the job is done" signal — not mere
# progress narration ("I finished the skill-local gate").
_COMPLETION_CLAIM_RE = re.compile(
    r"(?is)\b("
    r"all\s+(?:done|complete|finished|implemented)|"
    r"(?:task|implementation|plan|work|job|request)\s+(?:is\s+)?(?:done|complete|completed|finished)|"
    r"(?:fully|completely)\s+(?:done|complete|finished|implemented)|"
    r"nothing\s+(?:left|remaining|else)|"
    r"no\s+(?:remaining|outstanding)\s+(?:work|tasks?|items?)|"
    r"completed\s+(?:all|everything|the\s+(?:task|implementation|plan|request))"
    r")\b"
)

# Blocker / boundary language that identifies why autonomous work stopped.
_BLOCKER_RE = re.compile(
    r"(?is)\b("
    r"blocked|blocker|cannot\s+continue|can't\s+continue|"
    r"need(?:s)?\s+(?:your\s+)?(?:approval|authorization|permission|decision|go-ahead)|"
    r"(?:permission|approval|authorization)\s+(?:required|needed)|"
    r"(?:workspace|repository|repo|safety|external(?:-|\s)?system)\s+boundary|"
    r"outside\s+(?:the\s+)?(?:allowed\s+)?workspace|"
    r"out\s+of\s+scope|"
    r"remaining\s+(?:work|scope|items?|tasks?)|"
    r"still\s+(?:need(?:s)?|left|outstanding|remaining)|"
    r"not\s+(?:yet\s+)?(?:implemented|done|complete)|"
    r"pause(?:d)?\s+(?:here|at)|"
    r"stop(?:ping)?\s+(?:here|at\s+this)"
    r")\b"
)

# Explicit ask for the user's decision — required for an actionable handoff.
_DECISION_REQUEST_RE = re.compile(
    r"(?is)("
    r"\?|"
    r"\b(?:should\s+i|may\s+i|can\s+i|do\s+you\s+want\s+(?:me\s+)?to|"
    r"please\s+(?:confirm|approve|authorize|decide|choose)|"
    r"awaiting\s+(?:your\s+)?(?:decision|approval|authorization|go-ahead)|"
    r"let\s+me\s+know\s+(?:if|whether|how)|"
    r"which\s+(?:option|path|approach)\b)"
    r")"
)


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


def response_has_completion_claim(text: str) -> bool:
    """True when the final reply clearly claims the task is finished."""
    return bool(_COMPLETION_CLAIM_RE.search(text or ""))


def response_has_blocker_or_clarification(text: str) -> bool:
    """True when the reply identifies a blocker/boundary and asks to proceed.

    A self-contained handoff needs both signals: what stops autonomous work,
    and the decision the user must make. A question alone without blocker
    context, or a blocker summary without an ask, is not enough.
    """
    body = text or ""
    return bool(_BLOCKER_RE.search(body) and _DECISION_REQUEST_RE.search(body))


def is_valid_terminal_for_remaining_work(text: str) -> bool:
    """Final replies are valid only with a completion claim or a handoff ask."""
    return response_has_completion_claim(text) or response_has_blocker_or_clarification(
        text
    )


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


def turn_called_clarify(messages: Iterable[dict] | None) -> bool:
    """True if the latest assistant turn already invoked ``clarify``."""
    if not messages:
        return False
    for msg in reversed(list(messages)):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if _tool_call_name(tc) == "clarify":
                return True
        return False
    return False


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
    final_response: str | None = None,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    enabled: bool = True,
) -> Optional[str]:
    """Return a synthetic follow-up when remaining work ends without a handoff.

    Returns ``None`` when the guard should not fire (disabled, no remaining
    todos, clarify already asked, response already valid, or nudge budget
    exhausted).
    """
    if not enabled or attempts >= max_attempts:
        return None

    remaining = remaining_todo_items(todo_store)
    if not remaining:
        return None

    if turn_called_clarify(messages):
        return None

    if is_valid_terminal_for_remaining_work(final_response or ""):
        return None

    return (
        "[System: Task-tracking still shows unfinished work, but your last "
        "reply was neither a clear completion claim nor a self-contained "
        "boundary handoff.\n\n"
        "Outstanding items:\n"
        f"{_format_remaining(remaining)}\n\n"
        "A plain progress summary is not a valid terminal state here. Either:\n"
        "1. Finish the remaining work now, OR\n"
        "2. If you must pause at a permission, safety, workspace, repository, "
        "or external-system boundary, rewrite the reply as an actionable "
        "handoff that states (a) what you completed and verified, (b) what "
        "remains, (c) the exact boundary or blocker, (d) whether any live or "
        "external state changed, and (e) the decision or authorization needed "
        "to continue — and ask for that decision explicitly.\n\n"
        "Do not end with ambiguity about whether the task is complete, paused, "
        "blocked, or abandoned.]"
    )


__all__ = [
    "build_boundary_handoff_nudge",
    "has_remaining_work",
    "is_valid_terminal_for_remaining_work",
    "remaining_todo_items",
    "response_has_blocker_or_clarification",
    "response_has_completion_claim",
    "turn_called_clarify",
]
