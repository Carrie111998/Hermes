"""Deterministic safety and authority policy for durable Memory Duo writes."""

from __future__ import annotations

import re
from dataclasses import replace
from enum import Enum

from .contracts import (
    Authority,
    CandidateDecision,
    MemoryCandidate,
    MemoryRecord,
    Verification,
)
from .security import assert_safe_to_persist
from .vault import ParsedNote


class EventKind(str, Enum):
    TURN = "turn"
    EXPLICIT_REMEMBER = "explicit_remember"
    USER_CORRECTION = "user_correction"
    DECISION_CONFIRMED = "decision_confirmed"
    MILESTONE = "milestone"
    TASK_COMPLETE = "task_complete"
    SESSION_END = "session_end"
    DELEGATION_RESULT = "delegation_result"
    BUILTIN_MEMORY_WRITE = "builtin_memory_write"
    MANUAL_VAULT_EDIT = "manual_vault_edit"


_TRANSIENT_RE = re.compile(r"(?i)(?:terminal output|traceback|stack trace|temporary path|/tmp/|[A-Za-z]:\\[^\s]+)")


class MemoryPolicy:
    def evaluate(self, candidate: MemoryCandidate) -> CandidateDecision:
        try:
            assert_safe_to_persist(candidate.content)
        except ValueError:
            return CandidateDecision("reject", reason="secret credentials detected")
        if _TRANSIENT_RE.search(candidate.content) and candidate.metadata.get("event_kind") not in {
            EventKind.EXPLICIT_REMEMBER.value,
            EventKind.USER_CORRECTION.value,
        }:
            return CandidateDecision("discard", reason="transient operational output")

        event_kind = str(candidate.metadata.get("event_kind") or EventKind.TURN.value)
        if event_kind in {
            EventKind.EXPLICIT_REMEMBER.value,
            EventKind.USER_CORRECTION.value,
            EventKind.DECISION_CONFIRMED.value,
            EventKind.TASK_COMPLETE.value,
            EventKind.MANUAL_VAULT_EDIT.value,
        }:
            return CandidateDecision("promote", reason=event_kind)
        return CandidateDecision("stage", reason="requires confirmation or stronger evidence")

    def apply_user_edit(self, old: MemoryRecord, parsed: ParsedNote) -> MemoryRecord:
        return replace(
            old,
            content=parsed.body,
            memory_type=str(parsed.metadata.get("memory_type") or old.memory_type),
            scope=str(parsed.metadata.get("scope") or old.scope),
            authority=Authority.USER,
            verification=Verification.USER_CONFIRMED,
        )

    def merge_or_conflict(self, existing: list[MemoryRecord], candidate: MemoryCandidate) -> CandidateDecision:
        contradicted_id = str(candidate.metadata.get("contradicts") or "")
        match = next((record for record in existing if record.memory_id == contradicted_id), None)
        if match is not None and match.importance > 0.5:
            if candidate.authority is Authority.USER or candidate.metadata.get("event_kind") == EventKind.USER_CORRECTION.value:
                return CandidateDecision("supersede", memory_id=match.memory_id, reason="higher-authority user correction")
            return CandidateDecision("conflict", memory_id=match.memory_id, reason="important memory contradiction")
        return self.evaluate(candidate)
