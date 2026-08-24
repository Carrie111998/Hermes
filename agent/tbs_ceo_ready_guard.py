"""TBS CEO-readiness continuation guard.

This module is intentionally small and text-only.  It sits at the model-output
finalization seam and decides whether a TBS-flavored NOT CEO-READY answer is a
legitimate stopping condition or whether the agent should keep working inside the
safe envelope.
"""

from __future__ import annotations

import re
from typing import Any


SYNTHETIC_FLAG = "_tbs_ceo_ready_guard_synthetic"

_NOT_READY_RE = re.compile(
    r"\bNOT\s+CEO[-\s]?READY\b|\bNOT\s+READY\s+FOR\s+CEO\b|\bROUTING\s+INCOMPLETE\b",
    re.IGNORECASE,
)

# Terms that indicate the response reached a genuine stop/approval boundary.
# Keep this broad and conservative: false negatives cost one extra model turn;
# false positives can let unfinished work look final.
_HARD_STOP_RE = re.compile(
    r"\b(approval\s+(?:needed|required)|requires?\s+(?:Dave\s+)?approval|"
    r"awaits?\s+(?:Dave\s+)?approval|pending\s+(?:Dave\s+)?approval|"
    r"approve\s+(?:the|this|via|before)|hard[-\s]?stop|blocked|auth\s+blocked|"
    r"login\s+(?:is\s+)?(?:required|needed)|credential(?:s)?|password|"
    r"missing\s+(?:source|file|access|credential|business\s+direction|context)|"
    r"source\s+access\s+(?:is\s+)?missing|cannot\s+be\s+retrieved|"
    r"waiting\s+on\s+worker|worker\s+(?:is\s+)?(?:pending|running)|"
    r"Dave\s+(?:must|needs\s+to|can|should|reviews?|approve))\b",
    re.IGNORECASE,
)

_SAFE_WORK_RE = re.compile(
    r"\b(next\s+safe[-\s]?envelope\s+step|next\s+safe\s+step|"
    r"go\s+get\s+evidence|run\s+(?:worker|qa|tests?|verification)|"
    r"route\s+(?:the\s+)?(?:worker|next)|revise|build|draft|inspect|read|"
    r"verify|source\s+lookup|prepare\s+(?:the\s+)?approval\s+card|"
    r"continue\s+(?:safe|the\s+safe|working))\b",
    re.IGNORECASE,
)

_TBS_CONTEXT_RE = re.compile(
    r"\b(TBS|tbs-|worker[-\s]?first|routing\s+log|safe[-\s]?envelope)\b",
    re.IGNORECASE,
)

_CONTINUATION_NUDGE = """[SYSTEM CONTINUATION GUARD — TBS CEO-readiness]
Your last response labeled this TBS work NOT CEO-READY / ROUTING INCOMPLETE, but it did not state a hard-stop approval/source/credential/business-direction blocker.
Do not present the session as finished yet. Continue the next safe-envelope step now: gather missing evidence, route/run the worker, revise/build the deliverable, run QA/verification, or prepare an approval card.
Stop only when one of these is true:
1. the deliverable is CEO-ready and verified;
2. a hard approval gate is reached (send/publish/config/restart/commit/deploy/accounting mutation/delete/irreversible action);
3. required source access, credentials, files, or business direction is missing and cannot be retrieved safely;
4. the user explicitly asked for status-only/review-only output.
When you finally stop, make the exact stopping condition explicit."""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(value)


def response_needs_continuation(response_text: Any, *, user_message: Any = None) -> bool:
    """Return True when a TBS NOT CEO-READY response should not finalize.

    The guard activates only for TBS-shaped content and only when the response
    includes a not-ready/readiness-incomplete label. It lets real hard stops
    through, and otherwise nudges when the answer names safe follow-up work or
    simply leaves the not-ready state unresolved.
    """
    text = _as_text(response_text).strip()
    if not text or not _NOT_READY_RE.search(text):
        return False

    context = text + "\n" + _as_text(user_message)
    if not _TBS_CONTEXT_RE.search(context):
        return False

    if _HARD_STOP_RE.search(text):
        return False

    # If the response is not-ready and no hard stop was named, fail closed by
    # continuing. A listed safe next step makes the intent explicit; absence of
    # one is also a reason to continue rather than call the turn complete.
    return True


def build_continuation_nudge(response_text: Any, *, user_message: Any = None) -> str | None:
    if not response_needs_continuation(response_text, user_message=user_message):
        return None
    return _CONTINUATION_NUDGE
