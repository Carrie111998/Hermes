"""Compression attempt lifecycle — CRUD, state transitions, and staleness.

Bounded module owning the durable compression attempt state machine.
SessionDB methods in hermes_state.py become thin delegations to these
functions so the godfile retains only orchestration wiring.

Attempt states: pending → running → committed / aborted
Abort reasons: stale_parent_ended, lease_lost
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Optional

import logging

logger = logging.getLogger(__name__)


def create_compression_attempt(
    db: Any,
    *,
    attempt_id: str,
    session_key: str,
    parent_session_id: str,
    input_history_version: int,
    input_watermark: int,
    holder: str,
) -> None:
    """Insert a new compression attempt in pending state."""
    if not attempt_id or not session_key or not parent_session_id or not holder:
        raise ValueError("attempt_id/session_key/parent_session_id/holder required")
    now = time.time()

    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO compression_attempts "
            "(attempt_id, session_key, parent_session_id, input_history_version, "
            " input_watermark, holder, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                attempt_id,
                session_key,
                parent_session_id,
                int(input_history_version),
                int(input_watermark),
                holder,
                now,
                now,
            ),
        )

    db._execute_write(_do)


def transition_pending_to_running(db: Any, attempt_id: str) -> bool:
    """CAS pending → running. Returns True if transitioned."""
    if not attempt_id:
        return False
    now = time.time()

    def _do(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE compression_attempts SET state='running', updated_at=? "
            "WHERE attempt_id=? AND state='pending'",
            (now, attempt_id),
        )
        return cur.rowcount == 1

    return bool(db._execute_write(_do))


def transition_to_running(db: Any, attempt_id: str, holder: str) -> bool:
    """CAS pending → running with holder update. Returns True if transitioned."""
    if not attempt_id or not holder:
        return False
    now = time.time()

    def _do(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE compression_attempts SET state='running', holder=?, updated_at=? "
            "WHERE attempt_id=? AND state='pending' AND holder=?",
            (holder, now, attempt_id, holder),
        )
        # holder may have been placeholder == attempt_id; allow holder update
        if cur.rowcount == 0:
            cur2 = conn.execute(
                "UPDATE compression_attempts SET state='running', holder=?, updated_at=? "
                "WHERE attempt_id=? AND state='pending'",
                (holder, now, attempt_id),
            )
            return cur2.rowcount == 1
        return True

    return bool(db._execute_write(_do))


def get_compression_attempt(db: Any, attempt_id: str) -> Optional[Dict[str, Any]]:
    """Read a compression attempt by ID. Returns None if not found."""
    if not attempt_id:
        return None
    with db._read_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM compression_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row) if isinstance(row, sqlite3.Row) else dict(row)


def is_compression_attempt_stale(db: Any, attempt: Dict[str, Any] | str) -> Optional[bool]:
    """DB lineage tip check.

    Returns:
        True  — positively proven stale (tip.id != child_session_key)
        False — positively proven current (tip.id == child_session_key)
        None  — insufficient evidence (missing child, parent, family, source, or tip)
    """
    if isinstance(attempt, str):
        attempt = get_compression_attempt(db, attempt) or {}
    child = str(attempt.get("child_session_key") or "")
    if not child:
        return None
    parent_id = str(attempt.get("parent_session_id") or "")
    if not parent_id:
        return None
    with db._read_ctx() as conn:
        prow = conn.execute(
            "SELECT session_key, source FROM sessions WHERE id = ?", (parent_id,)
        ).fetchone()
        if prow is None:
            return None
        family = prow["session_key"] if isinstance(prow, sqlite3.Row) else prow[0]
        source = prow["source"] if isinstance(prow, sqlite3.Row) else prow[1]
        if not family or not source:
            return None
        tip = conn.execute(
            "SELECT id FROM sessions WHERE session_key = ? AND source = ? "
            "AND ended_at IS NULL ORDER BY COALESCE(last_activity_at, started_at) DESC, id DESC LIMIT 1",
            (family, source),
        ).fetchone()
        if tip is None:
            return None
        tip_id = tip["id"] if isinstance(tip, sqlite3.Row) else tip[0]
        return str(tip_id) != child


def get_compression_lock_holder(db: Any, session_id: str) -> Optional[str]:
    """Return the current (non-expired) holder for session_id, or None.

    Diagnostic helper — not used by the locking protocol itself.
    """
    if not session_id:
        return None
    now = time.time()
    row = db._conn.execute(
        "SELECT holder FROM compression_locks "
        "WHERE session_id = ? AND expires_at >= ?",
        (session_id, now),
    ).fetchone()
    if row is None:
        return None
    return row["holder"] if isinstance(row, sqlite3.Row) else row[0]


def build_attempt_status_response(db: Any, attempt_id: str) -> Dict[str, Any]:
    """Build a session.status response for an attempt_id.

    Returns a dict with: attempt_id, state, reason, session_key,
    parent_session_id, child_session_key, message_count, session_info,
    stale, input_watermark, input_history_version.
    """
    attempt = get_compression_attempt(db, attempt_id)
    if not attempt:
        return {"attempt_id": attempt_id, "state": "not_found"}

    child_id = str(attempt.get("child_session_key") or "")
    parent_id = str(attempt.get("parent_session_id") or "")
    family = str(attempt.get("session_key") or "")

    stale = is_compression_attempt_stale(db, attempt)

    out: Dict[str, Any] = {
        "attempt_id": attempt_id,
        "state": str(attempt.get("state") or ""),
        "reason": attempt.get("reason"),
        "session_key": family,
        "parent_session_id": parent_id,
        "child_session_key": child_id,
        "message_count": attempt.get("message_count"),
        "session_info": None,
        "stale": stale,
        "input_watermark": attempt.get("input_watermark"),
        "input_history_version": attempt.get("input_history_version"),
    }
    try:
        import json as _json

        sj = attempt.get("session_info_json")
        if sj:
            out["session_info"] = _json.loads(sj) if isinstance(sj, str) else sj
    except Exception:
        pass
    return out
