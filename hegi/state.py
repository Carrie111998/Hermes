"""Durable, transactional HEGI state."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS collector_cursor (
    source_db TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_episodes (
    meeting_id TEXT PRIMARY KEY,
    episode_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    episode_json TEXT NOT NULL,
    minutes_json TEXT,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS message_buffer (
    source_db TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_db, message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_buffer_pending
ON message_buffer(chat_id, consumed, timestamp);
CREATE TABLE IF NOT EXISTS report_delivery (
    meeting_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    platform_message_id TEXT,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (meeting_id, part_index)
);
CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    meeting_id TEXT,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    command TEXT NOT NULL,
    user_id TEXT NOT NULL,
    platform_message_id TEXT,
    raw_text TEXT NOT NULL,
    approved_at REAL NOT NULL,
    UNIQUE(platform_message_id)
);
CREATE TABLE IF NOT EXISTS approval_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    platform_message_id TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    last_error TEXT,
    workflow_state TEXT NOT NULL DEFAULT 'received',
    draft_id TEXT,
    idempotency_key TEXT,
    memory_id TEXT,
    memory_path TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_jobs_status
ON approval_jobs(status, created_at);
CREATE TABLE IF NOT EXISTS approval_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    details_json TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(job_id) REFERENCES approval_jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_approval_transitions_job
ON approval_transitions(job_id, id);
CREATE TABLE IF NOT EXISTS action_items (
    action_id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    item_json TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_items_open
ON action_items(status, fingerprint);
"""


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_approval_jobs(connection)

    @staticmethod
    def _migrate_approval_jobs(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(approval_jobs)").fetchall()
        }
        additions = {
            "workflow_state": "TEXT NOT NULL DEFAULT 'received'",
            "draft_id": "TEXT",
            "idempotency_key": "TEXT",
            "memory_id": "TEXT",
            "memory_path": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE approval_jobs ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_jobs_idempotency
            ON approval_jobs(idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_jobs_committed_meeting
            ON approval_jobs(meeting_id)
            WHERE memory_id IS NOT NULL
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_cursor(self, source_db: str) -> tuple[float, int]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT timestamp, message_id FROM collector_cursor WHERE source_db = ?",
                (source_db,),
            ).fetchone()
        return (float(row["timestamp"]), int(row["message_id"])) if row else (0.0, 0)

    def has_cursors(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM collector_cursor)"
            ).fetchone()
        return bool(row[0])

    def set_cursor(self, source_db: str, timestamp: float, message_id: int) -> None:
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collector_cursor(source_db, timestamp, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_db) DO UPDATE SET
                    timestamp=excluded.timestamp,
                    message_id=excluded.message_id,
                    updated_at=excluded.updated_at
                """,
                (source_db, timestamp, message_id, now),
            )

    def buffer_messages(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO message_buffer(
                    source_db, message_id, chat_id, timestamp, payload_json, consumed
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                [
                    (
                        str(message["source_db"]),
                        int(message["message_id"]),
                        str(message["chat_id"]),
                        float(message["timestamp"]),
                        json.dumps(message, ensure_ascii=False),
                    )
                    for message in messages
                ],
            )

    def buffered_messages(self, chat_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM message_buffer
                WHERE chat_id=? AND consumed=0 ORDER BY timestamp, message_id
                """,
                (str(chat_id),),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def consume_messages(self, source_keys: list[tuple[str, int]]) -> None:
        if not source_keys:
            return
        with self.connect() as connection:
            connection.executemany(
                "UPDATE message_buffer SET consumed=1 WHERE source_db=? AND message_id=?",
                source_keys,
            )

    def consume_range(self, chat_id: str, started_at: float, ended_at: float) -> None:
        """Consume every source copy represented by a merged episode."""
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE message_buffer SET consumed=1
                WHERE chat_id=? AND timestamp>=? AND timestamp<=?
                """,
                (str(chat_id), started_at, ended_at),
            )

    def save_episode(self, meeting_id: str, episode_hash: str, payload: dict[str, Any], status: str) -> bool:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_episodes(
                    meeting_id, episode_hash, status, episode_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    episode_hash,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def episode_by_id(self, meeting_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM processed_episodes WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_episode(
        self,
        meeting_id: str,
        *,
        status: str,
        minutes: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE processed_episodes SET status=?, minutes_json=COALESCE(?, minutes_json),
                    last_error=?, updated_at=? WHERE meeting_id=?
                """,
                (
                    status,
                    json.dumps(minutes, ensure_ascii=False) if minutes else None,
                    error,
                    time.time(),
                    meeting_id,
                ),
            )

    def delivered_parts(self, meeting_id: str) -> set[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT part_index FROM report_delivery WHERE meeting_id=? AND status='sent'",
                (meeting_id,),
            ).fetchall()
        return {int(row["part_index"]) for row in rows}

    def record_delivery(
        self,
        meeting_id: str,
        part_index: int,
        content_hash: str,
        *,
        status: str,
        platform_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO report_delivery(
                    meeting_id, part_index, content_hash, platform_message_id,
                    status, retry_count, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(meeting_id, part_index) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    platform_message_id=excluded.platform_message_id,
                    status=excluded.status,
                    retry_count=report_delivery.retry_count +
                        CASE WHEN excluded.status='failed' THEN 1 ELSE 0 END,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    meeting_id,
                    part_index,
                    content_hash,
                    platform_message_id,
                    status,
                    1 if status == "failed" else 0,
                    error,
                    time.time(),
                ),
            )

    def add_dead_letter(
        self, kind: str, payload: dict[str, Any], error: str, meeting_id: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO dead_letter(kind, meeting_id, payload_json, error, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, meeting_id, json.dumps(payload, ensure_ascii=False), error, time.time()),
            )

    def record_approval(
        self,
        meeting_id: str,
        command: str,
        user_id: str,
        raw_text: str,
        platform_message_id: str | None,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO approval_events(
                    meeting_id, command, user_id, platform_message_id, raw_text, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (meeting_id, command, user_id, platform_message_id, raw_text, time.time()),
            )
        return cursor.rowcount == 1

    def enqueue_approval_job(
        self,
        *,
        meeting_id: str,
        platform_message_id: str,
        project: str,
    ) -> bool:
        now = time.time()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM approval_jobs
                WHERE meeting_id=? AND status IN ('pending', 'processing')
                LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
            if active is not None:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO approval_jobs(
                    meeting_id, platform_message_id, project, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (meeting_id, platform_message_id, project, now, now),
            )
            connection.commit()
            return cursor.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def committed_approval_for_meeting(
        self, meeting_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approval_jobs
                WHERE meeting_id=? AND memory_id IS NOT NULL
                LIMIT 1
                """,
                (meeting_id,),
            ).fetchone()
        return dict(row) if row else None

    def pending_drafts_for_meeting(self, meeting_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approval_jobs
                WHERE meeting_id=? AND draft_id IS NOT NULL
                  AND memory_id IS NULL
                  AND workflow_state IN ('draft_created', 'draft_validated')
                ORDER BY created_at DESC
                """,
                (meeting_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def approval_job_for_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        query = """
            SELECT * FROM approval_jobs
            WHERE meeting_id=? AND draft_id IS NOT NULL
        """
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(query, (meeting_id,)).fetchone()
        return dict(row) if row else None

    def update_approval_workflow(
        self,
        job_id: int,
        state: str,
        *,
        details: dict[str, Any] | None = None,
        draft_id: str | None = None,
        idempotency_key: str | None = None,
        memory_id: str | None = None,
        memory_path: str | None = None,
    ) -> None:
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE approval_jobs SET workflow_state=?,
                    draft_id=COALESCE(?, draft_id),
                    idempotency_key=COALESCE(?, idempotency_key),
                    memory_id=COALESCE(?, memory_id),
                    memory_path=COALESCE(?, memory_path),
                    updated_at=?
                WHERE id=?
                """,
                (
                    state,
                    draft_id,
                    idempotency_key,
                    memory_id,
                    memory_path,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO approval_transitions(job_id, state, details_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    job_id,
                    state,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    now,
                ),
            )

    def approval_transitions(self, job_id: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT state FROM approval_transitions
                WHERE job_id=? ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [str(row["state"]) for row in rows]

    def resume_recoverable_approval_jobs(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE approval_jobs SET status='pending', updated_at=?
                WHERE status='failed'
                  AND workflow_state IN ('approved', 'committed', 'post_commit_failed')
                """,
                (time.time(),),
            )

    def claim_approval_job(
        self, *, stale_after_seconds: int = 300
    ) -> dict[str, Any] | None:
        now = time.time()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE approval_jobs SET status='pending', updated_at=?
                WHERE status='processing' AND updated_at<?
                """,
                (now, now - stale_after_seconds),
            )
            row = connection.execute(
                """
                SELECT * FROM approval_jobs
                WHERE status='pending' ORDER BY created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE approval_jobs SET status='processing', updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, row["id"]),
            )
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_approval_job(
        self,
        job_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError(f"invalid approval job status: {status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE approval_jobs SET status=?, result_json=?, last_error=?,
                    updated_at=? WHERE id=?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    time.time(),
                    job_id,
                ),
            )

    def mark_meeting_rejected(self, meeting_id: str) -> None:
        self.update_episode(meeting_id, status="rejected")

    def meeting_for_report_message(
        self, platform_message_id: str
    ) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT meeting_id FROM report_delivery
                WHERE platform_message_id=? AND status='sent'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (str(platform_message_id),),
            ).fetchone()
        return str(row["meeting_id"]) if row else None

    def latest_reported_meeting(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT meeting_id FROM processed_episodes
                WHERE status='reported' AND minutes_json IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        return str(row["meeting_id"]) if row else None

    def approval_job_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM approval_jobs GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
