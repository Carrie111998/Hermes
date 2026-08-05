"""Experiential ledger for Ebbinghaus retrieval misses, events, and later AGIASI flows.

This module owns event / retrieval-attempt writes. It does not call the store's
public ``remember`` / ``recall`` APIs from inside transactions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .models import (
    InsightValidationResult,
    RevisionResult,
)
from .policies import EbbinghausPolicies

logger = logging.getLogger(__name__)

_MAX_EVENT_FIELD_CHARS = 4000
_MAX_EVENT_PAYLOAD_CHARS = 32000


def normalize_query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _safe_json_payload(payload: Mapping[str, Any] | None) -> str:
    data = dict(payload or {})
    # Never persist opaque model scratch / chain-of-thought keys.
    blocked = {
        "chain_of_thought",
        "thinking",
        "reasoning",
        "scratchpad",
        "hidden_state",
        "raw_logits",
    }
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        key_text = str(key)
        if key_text.lower() in blocked:
            continue
        if isinstance(value, str) and len(value) > _MAX_EVENT_FIELD_CHARS:
            value = value[:_MAX_EVENT_FIELD_CHARS]
        cleaned[key_text] = value
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) > _MAX_EVENT_PAYLOAD_CHARS:
        encoded = json.dumps(
            {"truncated": True, "keys": sorted(cleaned.keys())[:32]},
            ensure_ascii=False,
        )
    return encoded


class EbbinghausExperienceLedger:
    """Append-only experiential events and retrieval-attempt bookkeeping."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now_fn: Callable[[], float],
        policies: EbbinghausPolicies,
        lock: threading.RLock,
    ) -> None:
        self._conn = conn
        self._now_fn = now_fn
        self.policies = policies
        self._lock = lock

    def record_event(
        self,
        event_type: str,
        *,
        memory_id: int | None = None,
        related_memory_id: int | None = None,
        belief_id: str = "",
        session_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO memory_events(
                    event_type, memory_id, related_memory_id, belief_id,
                    session_id, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_type),
                    memory_id,
                    related_memory_id,
                    str(belief_id or ""),
                    str(session_id or ""),
                    _safe_json_payload(payload),
                    float(self._now_fn()),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_retrieval_miss(
        self,
        *,
        query_hash: str,
        query_excerpt: str,
        query_cues: Sequence[str],
        direct_best_score: float,
        session_id: str = "",
    ) -> int:
        with self._lock:
            self._trim_unresolved_misses_locked()
            cur = self._conn.execute(
                """
                INSERT INTO retrieval_attempts(
                    query_hash, query_excerpt, query_cues, outcome,
                    top_memory_id, matched_miss_id, result_memory_ids,
                    direct_best_score, rescue_score, surprise,
                    resolved_at, created_at
                ) VALUES (?, ?, ?, 'miss', NULL, NULL, '[]', ?, 0.0, 0.0, NULL, ?)
                """,
                (
                    str(query_hash),
                    str(query_excerpt or ""),
                    json.dumps(list(query_cues), ensure_ascii=False),
                    float(direct_best_score),
                    float(self._now_fn()),
                ),
            )
            attempt_id = int(cur.lastrowid)
            self._conn.execute(
                """
                INSERT INTO memory_events(
                    event_type, memory_id, related_memory_id, belief_id,
                    session_id, payload, created_at
                ) VALUES ('retrieval_miss', NULL, NULL, '', ?, ?, ?)
                """,
                (
                    str(session_id or ""),
                    _safe_json_payload(
                        {
                            "attempt_id": attempt_id,
                            "query_hash": query_hash,
                            "direct_best_score": float(direct_best_score),
                            "cue_count": len(list(query_cues)),
                        }
                    ),
                    float(self._now_fn()),
                ),
            )
            self._conn.commit()
            return attempt_id

    def resolve_retrieval_miss(
        self,
        *,
        current_query_hash: str,
        current_cues: Sequence[str],
        rescued_memory_id: int,
        rescue_score: float,
        direct_best_score: float,
        session_id: str = "",
    ) -> dict[str, Any] | None:
        current_cue_set = {str(c).strip().lower() for c in current_cues if str(c).strip()}
        cutoff = float(self._now_fn()) - (
            float(self.policies.experience.miss_resolution_days) * 86400.0
        )
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT attempt_id, query_hash, query_cues, created_at
                FROM retrieval_attempts
                WHERE outcome = 'miss'
                  AND resolved_at IS NULL
                  AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
            best: tuple[float, float, int] | None = None  # overlap, created_at, id
            for row in rows:
                attempt_id = int(row["attempt_id"] if isinstance(row, sqlite3.Row) else row[0])
                old_hash = str(row["query_hash"] if isinstance(row, sqlite3.Row) else row[1])
                raw_cues = row["query_cues"] if isinstance(row, sqlite3.Row) else row[2]
                created_at = float(row["created_at"] if isinstance(row, sqlite3.Row) else row[3])
                try:
                    old_cues = {str(c).strip().lower() for c in json.loads(raw_cues or "[]")}
                except (TypeError, ValueError, json.JSONDecodeError):
                    old_cues = set()
                same_hash = old_hash == str(current_query_hash)
                union = current_cue_set | old_cues
                overlap = (
                    len(current_cue_set & old_cues) / max(1, len(union))
                    if union
                    else 0.0
                )
                if not (same_hash or overlap >= 0.45):
                    continue
                rank = (overlap if not same_hash else 1.0, created_at, attempt_id)
                if best is None or rank > best:
                    best = rank
            if best is None:
                return None
            attempt_id = best[2]
            resolution_gain = max(0.0, float(rescue_score) - float(direct_best_score))
            surprise = max(0.0, min(1.0, resolution_gain))
            now = float(self._now_fn())
            self._conn.execute(
                """
                UPDATE retrieval_attempts
                SET outcome = 'rescued',
                    matched_miss_id = attempt_id,
                    top_memory_id = ?,
                    result_memory_ids = ?,
                    rescue_score = ?,
                    surprise = ?,
                    resolved_at = ?
                WHERE attempt_id = ?
                """,
                (
                    int(rescued_memory_id),
                    json.dumps([int(rescued_memory_id)], ensure_ascii=False),
                    float(rescue_score),
                    float(surprise),
                    now,
                    attempt_id,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO memory_events(
                    event_type, memory_id, related_memory_id, belief_id,
                    session_id, payload, created_at
                ) VALUES ('retrieval_rescued', ?, NULL, '', ?, ?, ?)
                """,
                (
                    int(rescued_memory_id),
                    str(session_id or ""),
                    _safe_json_payload(
                        {
                            "attempt_id": attempt_id,
                            "rescue_score": float(rescue_score),
                            "surprise": float(surprise),
                        }
                    ),
                    now,
                ),
            )
            self._conn.commit()
            return {
                "attempt_id": attempt_id,
                "matched_miss_id": attempt_id,
                "surprise": surprise,
            }

    def revise_memory(
        self,
        *,
        memory_id: int,
        normalized_content: str,
        encoded: Mapping[str, Any],
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        confidence: float,
        test_query: str,
        source: str,
        session_id: str,
    ) -> RevisionResult:
        raise NotImplementedError("revise_memory lands in a later AGIASI task")

    def retract_memory(
        self,
        *,
        memory_id: int,
        reason: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError("retract_memory lands in a later AGIASI task")

    def belief_history(
        self,
        *,
        memory_id: int | None = None,
        belief_id: str = "",
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("belief_history lands in a later AGIASI task")

    def queue_correction_rehearsal(
        self,
        *,
        belief_id: str,
        old_memory_id: int,
        new_memory_id: int,
        test_query: str,
    ) -> int:
        raise NotImplementedError("queue_correction_rehearsal lands in a later AGIASI task")

    def association_preview(self, *, limit: int) -> dict[str, Any]:
        raise NotImplementedError("association_preview lands in a later AGIASI task")

    def propose_insight(
        self,
        *,
        association_id: str,
        hypothesis: str,
        source_memory_ids: Sequence[int],
        initial_confidence: float,
    ) -> dict[str, Any]:
        raise NotImplementedError("propose_insight lands in a later AGIASI task")

    def validate_insight(
        self,
        *,
        candidate_id: str,
        validation_method: str,
        evidence: Sequence[Mapping[str, Any]],
        validated_confidence: float,
        summary: str,
    ) -> InsightValidationResult:
        raise NotImplementedError("validate_insight lands in a later AGIASI task")

    def reject_insight(self, *, candidate_id: str, reason: str) -> dict[str, Any]:
        raise NotImplementedError("reject_insight lands in a later AGIASI task")

    def contest_dependents(
        self,
        *,
        source_memory_id: int,
        reason: str,
        visited: set[int] | None = None,
    ) -> list[int]:
        raise NotImplementedError("contest_dependents lands in a later AGIASI task")

    def _trim_unresolved_misses_locked(self) -> None:
        limit = int(self.policies.experience.max_unresolved_misses)
        count_row = self._conn.execute(
            """
            SELECT COUNT(*) AS count FROM retrieval_attempts
            WHERE outcome = 'miss' AND resolved_at IS NULL
            """
        ).fetchone()
        count = int(count_row["count"] if isinstance(count_row, sqlite3.Row) else count_row[0])
        overflow = count + 1 - limit
        if overflow <= 0:
            return
        old_ids = self._conn.execute(
            """
            SELECT attempt_id FROM retrieval_attempts
            WHERE outcome = 'miss' AND resolved_at IS NULL
            ORDER BY created_at ASC, attempt_id ASC
            LIMIT ?
            """,
            (overflow,),
        ).fetchall()
        for row in old_ids:
            attempt_id = int(row["attempt_id"] if isinstance(row, sqlite3.Row) else row[0])
            self._conn.execute(
                "DELETE FROM retrieval_attempts WHERE attempt_id = ?",
                (attempt_id,),
            )
