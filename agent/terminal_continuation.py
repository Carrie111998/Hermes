"""Pure policy for recovering Codex turns that stop while work is unfinished.

This module decides *whether* a native model terminal is a false stop. Runtime
adapters remain responsible for retry mechanics, transcript hygiene, streaming,
and accounting. Keeping the classifier pure makes the safety contract directly
testable and prevents provider adapters from drifting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable

MAX_TERMINAL_CONTINUATIONS = 2

CONTINUATION_NUDGE = (
    "[System: Your response says the current task is still incomplete. Continue "
    "the task now and complete the remaining work, using tools as needed. If "
    "you are genuinely blocked or waiting for user input, state that explicitly.]"
)

# Persisted by the original Codex acknowledgement-recovery path without an
# ephemeral metadata flag. Keep recognizing it so pre-upgrade sessions do not
# mistake transport scaffolding for a new human turn after resume/compaction.
LEGACY_CONTINUATION_NUDGE = (
    "[System: Continue now. Execute the required tool calls and only send your "
    "final answer after completing the task.]"
)

BUDGET_EXHAUSTED_NOTICE = (
    "PAUSED — automatic continuation budget exhausted while the response still "
    "indicated unfinished work. Say ‘continue’ to resume."
)

_HOUSEKEEPING_TOOLS = {
    "memory",
    "todo",
    "supermemory-save",
    "supermemory_store",
}

_ACTION_PATTERN = (
    r"(?:apply|analy[sz]|bring|build|check|continu|debug|delegate|delete|deploy|"
    r"execut|finish|find|fix|implement|inspect|install|kick\s+off|launch|"
    r"look\s+(?:at|into)|migrat|open|patch|port|read|reconcil|report\s+back|"
    r"rerun|review|run|scan|scaffold|search|set(?:ting)?\s+(?:it\s+)?up|start|"
    r"summari[sz]|test|updat|verif|writ)\w*"
)
_EXECUTION_REQUEST_RE = re.compile(
    r"\b(?:apply|build|complete|continu|finish|fix|implement|migrat|patch|port|"
    r"reconcil|updat|writ)\w*\b"
)
_REPORT_ONLY_RE = re.compile(r"\b(?:audit|assess|analy[sz]|report|review)\w*\b")
_ANNOUNCE_RE = re.compile(
    r"\b(?:i['’]ll|i\s+will|i(?:['’]m|\s+am)\s+going\s+to|"
    r"let\s+me(?!\s+know)|now\s+let\s+me|going\s+straight\s+to|"
    r"i\s+can\s+do\s+that|i\s+can\s+help\s+with\s+that|"
    r"now\s+i['’]ll|now\s+i(?:['’]m|\s+am)(?:\s+going\s+to)?|"
    r"next,?\s+i['’]ll|next,?\s+i\s+will)\b"
    r"(?:(?![,.:;!?\n]|\b(?:never|not)\b).){0,80}?\b"
    + _ACTION_PATTERN
    + r"\b",
    re.IGNORECASE,
)
_POST_TOOL_PROGRESS_RE = re.compile(
    r"\b(?:"
    r"i(?:['’]m|\s+am)\s+now|"
    r"now\s+i(?:['’]m|\s+am)|"
    r"next,?\s+i(?:['’]ll|\s+will)|"
    r"i(?:['’]ll|\s+will)\s+now"
    r")\s+(?!(?:done|unable|finished|complete)\b)"
    r"(?:(?![.!?\n])\S+\s+){0,8}"
    + _ACTION_PATTERN
    + r"\b",
    re.IGNORECASE,
)
_UNFINISHED_RE = re.compile(
    r"\b(?:remaining\s+work|unfinished|still\s+failing|tests?\s+(?:are\s+)?"
    r"still\s+fail|not\s+(?:complete|finished|ready|promotable)|"
    r"still\s+(?:need|needs|required)|i\s+still\s+need)\b",
    re.IGNORECASE,
)
_BLOCK_OR_WAIT_RE = re.compile(
    r"\b(?:wait(?:ing)?\s+(?:for|on)\s+(?:you|your)\b|"
    r"awaiting\s+(?:approval|confirmation|input)|blocked\s+(?:on|by)|"
    r"cannot\s+continue|can['’]t\s+continue|need\s+your\s+"
    r"(?:approval|confirmation|credentials?|input|password|token)|"
    r"unable\s+to\s+(?:continue|run|execute|access|complete)|"
    r"waiting\s+(?:for|on)\s+[^.!?\n]{0,100}\b"
    r"(?:finish|complete|return|result)|"
    r"(?:build|deploy|job|task|command|process|tests?(?:\s+suite)?)\s+"
    r"(?:is\s+)?(?:still\s+running|in\s+flight|hasn['’]t\s+returned|"
    r"has\s+not\s+returned)|"
    r"process\s+is\s+still\s+running|unavailable\s+(?:dependency|service)|"
    r"let\s+me\s+know|would\s+you\s+like|if\s+you['’]?d?\s+"
    r"(?:like|prefer)|if\s+you\s+want|next\s+steps?\s+could\s+include)\b",
    re.IGNORECASE,
)
_REFUSAL_OR_IDIOM_RE = re.compile(
    r"\b(?:i['’]ll\s+never|i\s+will\s+(?:never|not)|check\s+in(?:\s+with)?|"
    r"run\s+through|open\s+with|build\s+on|read\s+you|"
    r"walk\s+you\s+through)\b",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"\b(?:done|finished|implementation\s+is\s+complete|all\s+tests?\s+pass|"
    r"requested\s+(?:audit|review|task)\s+is\s+complete)\b",
    re.IGNORECASE,
)
_ALLOWED_FINISH_REASONS = {None, "", "stop", "completed"}


class ContinuationReason(str, Enum):
    NONE = "none"
    INITIAL_INTENT_ACK = "initial_intent_ack"
    POST_TOOL_IMMEDIATE_ACTION = "post_tool_immediate_action"
    POST_TOOL_EXPLICIT_UNFINISHED = "post_tool_explicit_unfinished"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class ContinuationFacts:
    runtime: str
    terminal_completed: bool
    finish_reason: str | None
    substantive_tool_count: int
    assistant_text: str
    user_text: str
    workspace_scoped: bool
    continuation_attempts: int = 0
    interrupted: bool = False
    transport_error: bool = False
    pending_background: bool = False
    allow_non_codex: bool = False


def _tool_name_by_call_id(messages: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            name = function.get("name") if isinstance(function, dict) else None
            name = name or call.get("name")
            if call_id and name:
                names[call_id] = str(name)
    return names


def count_substantive_tools(messages: Iterable[Dict[str, Any]]) -> int:
    """Count current-turn tool results, excluding known state-maintenance tools.

    Unknown tool names count as substantive. This avoids disabling recovery for
    provider-native or newly added execution tools while ensuring todo/memory
    bookkeeping alone cannot trigger a post-tool continuation.
    """
    rows = list(messages)
    names = _tool_name_by_call_id(rows)
    count = 0
    for message in rows:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        name = str(message.get("name") or names.get(call_id) or "").lower()
        if name in _HOUSEKEEPING_TOOLS:
            continue
        count += 1
    return count


def _execution_posture(user_text: str) -> bool:
    text = user_text.lower()
    if _EXECUTION_REQUEST_RE.search(text):
        return True
    return not _REPORT_ONLY_RE.search(text) and bool(_ACTION_PATTERN_RE.search(text))


_ACTION_PATTERN_RE = re.compile(r"\b" + _ACTION_PATTERN + r"\b", re.IGNORECASE)


def classify_terminal_continuation(facts: ContinuationFacts) -> ContinuationReason:
    """Return a reasoned continuation decision for a completed native turn."""
    if (
        not facts.terminal_completed
        or facts.interrupted
        or facts.transport_error
        or facts.pending_background
        or (facts.finish_reason or "").lower() not in _ALLOWED_FINISH_REASONS
    ):
        return ContinuationReason.NONE

    if not facts.allow_non_codex:
        if facts.runtime not in {"codex_responses", "codex_app_server"}:
            return ContinuationReason.NONE
        if not facts.workspace_scoped:
            return ContinuationReason.NONE

    text = (facts.assistant_text or "").strip()
    if not text:
        return ContinuationReason.NONE
    lower = text.lower()
    tail = lower[-2400:]

    if (
        facts.pending_background
        or "?" in lower
        or _BLOCK_OR_WAIT_RE.search(lower)
        or _REFUSAL_OR_IDIOM_RE.search(lower)
    ):
        return ContinuationReason.NONE

    unfinished = bool(_UNFINISHED_RE.search(tail))
    if _DONE_RE.search(tail) and not unfinished:
        return ContinuationReason.NONE

    reason = ContinuationReason.NONE
    if facts.substantive_tool_count <= 0:
        if len(text) <= 1200 and _ANNOUNCE_RE.search(lower):
            reason = ContinuationReason.INITIAL_INTENT_ACK
    elif _POST_TOOL_PROGRESS_RE.search(tail) or _ANNOUNCE_RE.search(tail):
        reason = ContinuationReason.POST_TOOL_IMMEDIATE_ACTION
    elif (
        unfinished
        and _execution_posture(facts.user_text)
        and _ACTION_PATTERN_RE.search(tail)
    ):
        reason = ContinuationReason.POST_TOOL_EXPLICIT_UNFINISHED

    if reason is not ContinuationReason.NONE and (
        facts.continuation_attempts >= MAX_TERMINAL_CONTINUATIONS
    ):
        return ContinuationReason.BUDGET_EXHAUSTED
    return reason
