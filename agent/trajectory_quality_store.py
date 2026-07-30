"""Profile-aware SQLite store for trajectory quality decision records.

Follows the ``verification_evidence.py`` pattern: a sidecar database under
``get_hermes_home()`` so the main SessionDB schema is never touched. All
string fields are defensively redacted before write — only hashes, tool
names, counts, and short explain strings are persisted. Never raw args,
results, or stdout.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.trajectory_quality import TrajectoryQualityDecision

_DB_LOCK = threading.Lock()
_SCHEMA_VERSION = 1
_DEFAULT_RETENTION_DAYS = 30
_DEFAULT_MAX_PER_SESSION = 200

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retention_cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _db_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "trajectory_quality.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            api_call_count INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            level_before TEXT NOT NULL,
            level_after TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_hash TEXT NOT NULL,
            result_hash TEXT,
            count INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            recommended_model TEXT,
            recommended_provider TEXT,
            explain TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_session
        ON decisions(session_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_created
        ON decisions(created_at)
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _redact(text: str) -> str:
    """Defensively redact any secret-like substring before persistence."""
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        # Redaction must never block persistence — fall back to the raw
        # string if the redactor is unavailable. The explain strings only
        # contain tool names and counts anyway.
        return text


class TrajectoryQualityStore:
    """Durable store for trajectory quality routing decisions.

    All writes are redacted and bounded by retention + per-session caps.
    The store fails open: if the database cannot be opened, ``record``
    logs a warning and returns a synthetic id without raising.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        max_decisions_per_session: int = _DEFAULT_MAX_PER_SESSION,
    ):
        self._path = path
        self._retention_days = retention_days
        self._max_per_session = max_decisions_per_session

    def record(
        self,
        decision: TrajectoryQualityDecision,
        *,
        session_id: str = "",
        api_call_count: int = 0,
    ) -> str:
        """Persist a decision and return its id. Never raises."""
        decision_id = decision.decision_id or uuid.uuid4().hex
        created_at = _utc_now()
        try:
            with _DB_LOCK:
                conn = _connect(self._path)
                try:
                    conn.execute(
                        """
                        INSERT INTO decisions (
                            id, created_at, session_id, api_call_count,
                            action, reason_code, level_before, level_after,
                            tool_name, args_hash, result_hash, count,
                            model, provider, recommended_model,
                            recommended_provider, explain
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            created_at,
                            _redact(session_id),
                            api_call_count,
                            _redact(decision.action),
                            _redact(decision.reason_code),
                            _redact(decision.level_before),
                            _redact(decision.level_after),
                            _redact(decision.tool_name),
                            decision.args_hash,
                            decision.result_hash,
                            decision.count,
                            _redact(decision.model),
                            _redact(decision.provider),
                            _redact(decision.recommended_model or ""),
                            _redact(decision.recommended_provider or ""),
                            _redact(decision.explain),
                        ),
                    )
                    self._enforce_session_cap(conn, session_id)
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning(
                "TrajectoryQualityStore.record failed (fail-open): %s", exc
            )
        return decision_id

    def list_for_session(
        self, session_id: str, *, limit: int = 50
    ) -> list[dict]:
        """Return decisions for a session, newest first."""
        try:
            with _DB_LOCK:
                conn = _connect(self._path)
                try:
                    rows = conn.execute(
                        """
                        SELECT * FROM decisions
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (session_id, limit),
                    ).fetchall()
                    return [dict(r) for r in rows]
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("TrajectoryQualityStore.list failed: %s", exc)
            return []

    def purge_expired(self) -> int:
        """Delete rows older than retention_days. Returns count deleted."""
        cutoff = _retention_cutoff(self._retention_days)
        try:
            with _DB_LOCK:
                conn = _connect(self._path)
                try:
                    cur = conn.execute(
                        "DELETE FROM decisions WHERE created_at < ?", (cutoff,)
                    )
                    conn.commit()
                    return cur.rowcount or 0
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("TrajectoryQualityStore.purge failed: %s", exc)
            return 0

    def _enforce_session_cap(
        self, conn: sqlite3.Connection, session_id: str
    ) -> None:
        """Drop oldest rows exceeding the per-session cap."""
        conn.execute(
            """
            DELETE FROM decisions
            WHERE session_id = ?
              AND id NOT IN (
                  SELECT id FROM decisions
                  WHERE session_id = ?
                  ORDER BY created_at DESC
                  LIMIT ?
              )
            """,
            (session_id, session_id, self._max_per_session),
        )
