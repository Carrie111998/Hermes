"""Durable claim, cursor, retry, and receipt state for Kanban projection."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.codex.kanban_settings import KanbanProjectionSettings


_NOTIFICATION_PHASES = frozenset({"captured", "needs_user", "output_ready", "failed"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectionReceiptStore:
    """Own projection queue, per-job claims, retry state, and receipts."""

    def __init__(
        self,
        bridge_db_path: Path,
        settings: KanbanProjectionSettings,
        owner_id: str,
    ):
        self.bridge_db_path = Path(bridge_db_path)
        self.settings = settings
        self.owner_id = owner_id
        self._initialize()

    def source(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.bridge_db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self.source() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bridge_projection_queue (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    hermes_job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bridge_projection_jobs (
                    hermes_job_id TEXT PRIMARY KEY,
                    kanban_task_id TEXT,
                    projection_cursor INTEGER NOT NULL DEFAULT 0,
                    notification_cursor TEXT,
                    claim_owner TEXT,
                    claim_expires_at INTEGER,
                    last_error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    retry_state TEXT NOT NULL DEFAULT 'idle',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bridge_projection_receipts (
                    event_id TEXT PRIMARY KEY,
                    hermes_job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    notification_eligible INTEGER NOT NULL DEFAULT 0,
                    projected_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bridge_projection_receipts_job_sequence
                    ON bridge_projection_receipts(hermes_job_id, sequence);
                CREATE TRIGGER IF NOT EXISTS bridge_events_projection_queue
                AFTER INSERT ON bridge_events
                BEGIN
                    INSERT OR IGNORE INTO bridge_projection_queue (
                        event_id, hermes_job_id, created_at
                    ) VALUES (NEW.event_id, NEW.hermes_job_id, NEW.created_at);
                END;
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(bridge_projection_jobs)"
                ).fetchall()
            }
            for name, definition in (
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("next_retry_at", "TEXT"),
                ("retry_state", "TEXT NOT NULL DEFAULT 'idle'"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE bridge_projection_jobs ADD COLUMN {name} {definition}"
                    )
            db.execute(
                """
                INSERT OR IGNORE INTO bridge_projection_queue (
                    event_id, hermes_job_id, created_at
                )
                SELECT event_id, hermes_job_id, created_at
                FROM bridge_events
                ORDER BY rowid
                """
            )

    def pending(self, db: sqlite3.Connection) -> list[sqlite3.Row]:
        return db.execute(
            """
            SELECT q.sequence, e.event_id, e.hermes_job_id, e.payload_json,
                   e.created_at, j.workspace, j.origin_json,
                   j.final_result, j.artifacts_json
            FROM bridge_projection_queue q
            JOIN bridge_events e ON e.event_id = q.event_id
            JOIN bridge_jobs j ON j.hermes_job_id = e.hermes_job_id
            LEFT JOIN bridge_projection_receipts r ON r.event_id = e.event_id
            WHERE r.event_id IS NULL
            ORDER BY q.sequence
            """
        ).fetchall()

    def claim_job(self, job_id: str) -> bool:
        now = int(time.time())
        expires = now + self.settings.stale_claim_seconds
        with self.source() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO bridge_projection_jobs (
                    hermes_job_id, updated_at
                ) VALUES (?, ?)
                """,
                (job_id, _utc_now()),
            )
            row = db.execute(
                "SELECT claim_owner, claim_expires_at FROM bridge_projection_jobs "
                "WHERE hermes_job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                row["claim_owner"]
                and row["claim_owner"] != self.owner_id
                and int(row["claim_expires_at"] or 0) > now
            ):
                return False
            db.execute(
                """
                UPDATE bridge_projection_jobs
                SET claim_owner = ?, claim_expires_at = ?, updated_at = ?
                WHERE hermes_job_id = ?
                """,
                (self.owner_id, expires, _utc_now(), job_id),
            )
        return True

    def mapped_task_id(self, job_id: str) -> str | None:
        with self.source() as db:
            row = db.execute(
                "SELECT kanban_task_id FROM bridge_projection_jobs "
                "WHERE hermes_job_id = ?",
                (job_id,),
            ).fetchone()
        return str(row["kanban_task_id"]) if row and row["kanban_task_id"] else None

    def store_mapping(self, job_id: str, task_id: str) -> None:
        with self.source() as db:
            db.execute(
                """
                UPDATE bridge_projection_jobs
                SET kanban_task_id = ?, last_error = NULL, updated_at = ?
                WHERE hermes_job_id = ? AND claim_owner = ?
                """,
                (task_id, _utc_now(), job_id, self.owner_id),
            )

    def notification_eligible(self, db: sqlite3.Connection, row: sqlite3.Row) -> bool:
        event = json.loads(row["payload_json"])
        phase = str(event.get("phase") or "")
        if phase in _NOTIFICATION_PHASES:
            return True
        if phase != "done":
            return False
        prior_output = db.execute(
            """
            SELECT 1
            FROM bridge_projection_receipts r
            JOIN bridge_events e ON e.event_id = r.event_id
            WHERE r.hermes_job_id = ? AND e.phase = 'output_ready'
            LIMIT 1
            """,
            (row["hermes_job_id"],),
        ).fetchone()
        return prior_output is None

    def record_success(self, row: sqlite3.Row, notification_eligible: bool) -> None:
        now = _utc_now()
        with self.source() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO bridge_projection_receipts (
                    event_id, hermes_job_id, sequence,
                    notification_eligible, projected_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["event_id"],
                    row["hermes_job_id"],
                    row["sequence"],
                    1 if notification_eligible else 0,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE bridge_projection_jobs
                SET projection_cursor = MAX(projection_cursor, ?),
                    notification_cursor = CASE WHEN ? THEN ? ELSE notification_cursor END,
                    claim_owner = NULL, claim_expires_at = NULL,
                    last_error = NULL, retry_count = 0, next_retry_at = NULL,
                    retry_state = 'idle', updated_at = ?
                WHERE hermes_job_id = ?
                """,
                (
                    row["sequence"],
                    1 if notification_eligible else 0,
                    row["event_id"],
                    now,
                    row["hermes_job_id"],
                ),
            )

    def record_error(self, job_id: str, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {str(exc)}"[:500]
        with self.source() as db:
            db.execute(
                """
                INSERT INTO bridge_projection_jobs (
                    hermes_job_id, last_error, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(hermes_job_id) DO UPDATE SET
                    claim_owner = NULL, claim_expires_at = NULL,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (job_id, message, _utc_now()),
            )

    def record_retry_state(
        self,
        retry_count: int,
        next_retry_at: str | None,
        state: str,
    ) -> None:
        """Persist worker retry state for every job with pending events."""

        now = _utc_now()
        with self.source() as db:
            job_ids = {
                str(row["hermes_job_id"]) for row in self.pending(db)
            }
            if not job_ids:
                job_ids = {
                    str(row["hermes_job_id"])
                    for row in db.execute(
                        "SELECT hermes_job_id FROM bridge_projection_jobs"
                    ).fetchall()
                }
            for job_id in job_ids:
                db.execute(
                    """
                    INSERT INTO bridge_projection_jobs (
                        hermes_job_id, retry_count, next_retry_at, retry_state, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(hermes_job_id) DO UPDATE SET
                        retry_count = excluded.retry_count,
                        next_retry_at = excluded.next_retry_at,
                        retry_state = excluded.retry_state,
                        updated_at = excluded.updated_at
                    """,
                    (job_id, retry_count, next_retry_at, state[:40], now),
                )

    def status(self) -> dict[str, Any]:
        """Return operator-readable projection lag, cursor, error, and retry state."""

        with self.source() as db:
            pending_count = int(
                db.execute(
                    """
                    SELECT count(1)
                    FROM bridge_projection_queue q
                    LEFT JOIN bridge_projection_receipts r ON r.event_id = q.event_id
                    WHERE r.event_id IS NULL
                    """
                ).fetchone()[0]
            )
            cursor = int(
                db.execute(
                    "SELECT COALESCE(MAX(projection_cursor), 0) FROM bridge_projection_jobs"
                ).fetchone()[0]
            )
            job = db.execute(
                """
                SELECT last_error, retry_count, next_retry_at, retry_state, updated_at
                FROM bridge_projection_jobs
                WHERE last_error IS NOT NULL OR retry_state != 'idle'
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
            receipt_count = int(
                db.execute("SELECT count(1) FROM bridge_projection_receipts").fetchone()[0]
            )
        return {
            "enabled": True,
            "board": self.settings.board,
            "pending_count": pending_count,
            "projection_cursor": cursor,
            "receipt_count": receipt_count,
            "last_error": job["last_error"] if job else None,
            "retry_count": int(job["retry_count"] or 0) if job else 0,
            "next_retry_at": job["next_retry_at"] if job else None,
            "retry_state": str(job["retry_state"] or "idle") if job else "idle",
            "updated_at": job["updated_at"] if job else None,
        }

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        with self.source() as db:
            row = db.execute(
                "SELECT * FROM bridge_projection_jobs WHERE hermes_job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_receipts(self, job_id: str) -> list[dict[str, Any]]:
        with self.source() as db:
            rows = db.execute(
                "SELECT * FROM bridge_projection_receipts "
                "WHERE hermes_job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]
