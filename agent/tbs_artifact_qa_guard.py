"""TBS artifact QA finalization guard.

This guard is deliberately text-only at the finalization seam.  It does not open
files or inspect workbook bytes inside the generic conversation loop; instead it
requires evidence that the separate workbook validator already ran before a TBS
client/CFO XLSX workbook can be called complete.
"""
from __future__ import annotations

import re
from typing import Any

SYNTHETIC_FLAG = "_tbs_artifact_qa_guard_synthetic"

_XLSX_RE = re.compile(r"\b\S+\.xlsx\b|\b(workbook|spreadsheet|Excel|XLSX)\b", re.IGNORECASE)
_FINANCE_FORECAST_RE = re.compile(
    r"\b(CFO|client[-\s]?facing|finance|financial|cash[-\s]?flow|cash\s+forecast|forecast|projection|13[-\s]?week|reconciliation|workpaper)\b",
    re.IGNORECASE,
)
_COMPLETE_CLAIM_RE = re.compile(
    r"\b(done|complete(?:d)?|ready|client[-\s]?ready|CEO[-\s]?ready|CFO[-\s]?ready|final(?:ized)?|built|created|generated|delivered|attached|handoff)\b",
    re.IGNORECASE,
)
_NOT_READY_RE = re.compile(
    r"\b(NOT\s+CLIENT[-\s]?READY|NOT\s+CEO[-\s]?READY|MODEL\s+INCOMPLETE|FORMULAS\s+FLATTENED|not\s+ready|blocked|approval\s+(?:needed|required))\b",
    re.IGNORECASE,
)
_VALIDATOR_EVIDENCE_RE = re.compile(
    r"tbs_workbook_qa_validator\.py|mechanical\s+workbook\s+QA\s+validator|workbook\s+QA\s+validator",
    re.IGNORECASE,
)
_VALIDATOR_PASS_RE = re.compile(
    r"\b(status['\"]?\s*[:=]\s*['\"]?PASS['\"]?|validator\s+(?:self-test\s+)?passed|workbook\s+QA\s+validator\s+passed|\bok['\"]?\s*[:=]\s*(?:true|True))\b",
    re.IGNORECASE,
)
_TBS_CONTEXT_RE = re.compile(r"\b(TBS|tbs-|Dave|client[-\s]?facing|CEO[-\s]?ready|CFO[-\s]?ready)\b", re.IGNORECASE)

_CONTINUATION_NUDGE = """[SYSTEM CONTINUATION GUARD — TBS artifact QA]
Your last response appears to call a TBS client/CFO finance Excel workbook complete, but the session/final response does not show a passing `tbs_workbook_qa_validator.py` result.
Do not present the workbook as complete yet. Continue the safe next step now: run the workbook QA validator against the `.xlsx` artifact, revise/rebuild if it fails, or relabel the artifact `NOT CLIENT-READY / MODEL INCOMPLETE` or `NOT CEO-READY / FORMULAS FLATTENED` with the exact blocker.
Stop only when the final answer includes validator pass evidence, or when it explicitly labels the workbook not ready with the failed check/source blocker."""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(value)


def _recent_messages_text(messages: Any, *, max_messages: int = 32) -> str:
    if not isinstance(messages, list):
        return _as_text(messages)
    parts: list[str] = []
    for msg in messages[-max_messages:]:
        if not isinstance(msg, dict):
            parts.append(_as_text(msg))
            continue
        if msg.get("role") == "system":
            continue
        parts.append(_as_text(msg.get("content")))
    return "\n".join(p for p in parts if p)


def has_passing_workbook_validator_evidence(*texts: Any) -> bool:
    """Return True when the context includes the validator and a pass marker."""
    context = "\n".join(_as_text(t) for t in texts if t is not None)
    return bool(_VALIDATOR_EVIDENCE_RE.search(context) and _VALIDATOR_PASS_RE.search(context))


def response_needs_artifact_qa(
    response_text: Any,
    *,
    user_message: Any = None,
    recent_messages: Any = None,
) -> bool:
    """Whether a final response should be held for workbook QA evidence.

    This intentionally guards only the high-risk class Dave named: TBS/client/CFO
    finance forecast XLSX completion claims. It does not block generic scratch
    spreadsheets, drafts already labeled not-ready, or responses that show the
    separate validator passed.
    """
    response = _as_text(response_text).strip()
    if not response:
        return False
    user = _as_text(user_message)
    recent = _recent_messages_text(recent_messages)
    context = "\n".join([response, user, recent])

    if _NOT_READY_RE.search(response):
        return False
    if not _TBS_CONTEXT_RE.search(context):
        return False
    if not (_XLSX_RE.search(response) or re.search(r"\.xlsx\b", context, re.IGNORECASE)):
        return False
    if not _FINANCE_FORECAST_RE.search(context):
        return False
    if not _COMPLETE_CLAIM_RE.search(response):
        return False
    if has_passing_workbook_validator_evidence(response, recent):
        return False
    return True


def build_artifact_qa_nudge(
    response_text: Any,
    *,
    user_message: Any = None,
    recent_messages: Any = None,
) -> str | None:
    if not response_needs_artifact_qa(response_text, user_message=user_message, recent_messages=recent_messages):
        return None
    return _CONTINUATION_NUDGE
