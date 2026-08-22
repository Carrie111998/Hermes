"""Durable SQLite state for Codex bridge jobs, replies, and public events."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

from gateway.codex.protocol import (
    TERMINAL_PHASES,
    BridgeMapping,
    BridgeOrigin,
    BridgeReply,
    BridgeReplyMapping,
    BridgeRequest,
    CaptureResult,
    PendingQuestion,
    ProgressEvent,
    ReplyCaptureResult,
    _utc_now,
    request_fingerprint,
)


def _validate_reply_origin(stored: Mapping[str, str], reply: BridgeOrigin) -> None:
    """Require a reply to come from the same channel conversation and user."""

    current = reply.as_dict()
    for key in ("type", "conversation_id", "user_id", "thread_id"):
        expected = stored.get(key)
        if expected and current.get(key) != expected:
            raise ValueError("Codex bridge reply origin does not match the pending request")


class BridgeStore:
    """Small durable SQLite store for mapping and compact task events."""

    def __init__(self, path: Path | None = None):
        self.path = path or (get_hermes_home() / "codex_bridge" / "state.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bridge_jobs (
                    hermes_job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL DEFAULT '',
                    codex_thread_id TEXT,
                    origin_json TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    final_result TEXT,
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    owner_instance_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bridge_events (
                    event_id TEXT PRIMARY KEY,
                    hermes_job_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hermes_job_id) REFERENCES bridge_jobs(hermes_job_id)
                );
                CREATE INDEX IF NOT EXISTS bridge_events_job_created
                    ON bridge_events(hermes_job_id, created_at);
                CREATE TABLE IF NOT EXISTS bridge_pending_questions (
                    prompt_id TEXT PRIMARY KEY,
                    hermes_job_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    origin_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply_id TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    FOREIGN KEY(hermes_job_id) REFERENCES bridge_jobs(hermes_job_id)
                );
                CREATE INDEX IF NOT EXISTS bridge_questions_job_status
                    ON bridge_pending_questions(hermes_job_id, status, created_at);
                CREATE TABLE IF NOT EXISTS bridge_replies (
                    reply_id TEXT PRIMARY KEY,
                    hermes_job_id TEXT NOT NULL,
                    prompt_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    origin_json TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    final_result TEXT,
                    owner_instance_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(hermes_job_id) REFERENCES bridge_jobs(hermes_job_id),
                    FOREIGN KEY(prompt_id) REFERENCES bridge_pending_questions(prompt_id)
                );
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(bridge_jobs)")
            }
            if "artifacts_json" not in columns:
                db.execute(
                    "ALTER TABLE bridge_jobs ADD COLUMN "
                    "artifacts_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "request_fingerprint" not in columns:
                db.execute(
                    "ALTER TABLE bridge_jobs ADD COLUMN "
                    "request_fingerprint TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _mapping(row: sqlite3.Row) -> BridgeMapping:
        return BridgeMapping(
            hermes_job_id=row["hermes_job_id"],
            idempotency_key=row["idempotency_key"],
            request_fingerprint=row["request_fingerprint"],
            codex_thread_id=row["codex_thread_id"],
            origin=json.loads(row["origin_json"]),
            workspace=row["workspace"],
            phase=row["phase"],
            final_result=row["final_result"],
            artifacts=tuple(json.loads(row["artifacts_json"] or "[]")),
            owner_instance_id=row["owner_instance_id"],
            updated_at=row["updated_at"],
        )

    def capture(
        self,
        request: BridgeRequest,
        *,
        owner_instance_id: str,
        stale_recovery_seconds: int,
    ) -> CaptureResult:
        now = _utc_now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    INSERT INTO bridge_jobs (
                        hermes_job_id, idempotency_key, request_fingerprint,
                        origin_json, workspace,
                        phase, owner_instance_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'captured', ?, ?, ?)
                    """,
                    (
                        request.hermes_job_id,
                        request.idempotency_key,
                        request_fingerprint(request.prompt),
                        json.dumps(request.origin.as_dict(), sort_keys=True),
                        request.workspace,
                        owner_instance_id,
                        now,
                        now,
                    ),
                )
                row = db.execute(
                    "SELECT * FROM bridge_jobs WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                db.commit()
                return CaptureResult(self._mapping(row), True)
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT * FROM bridge_jobs WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if row is None:
                    db.rollback()
                    raise
                mapping = self._mapping(row)
                if (
                    mapping.hermes_job_id != request.hermes_job_id
                    or mapping.origin != request.origin.as_dict()
                    or mapping.workspace != request.workspace
                    or (
                        mapping.request_fingerprint
                        and mapping.request_fingerprint
                        != request_fingerprint(request.prompt)
                    )
                ):
                    db.rollback()
                    raise ValueError(
                        "Codex bridge idempotency key conflicts with another request"
                    )
                if (
                    mapping.phase in TERMINAL_PHASES
                    or mapping.phase == "needs_user"
                    or mapping.owner_instance_id == owner_instance_id
                ):
                    db.commit()
                    return CaptureResult(mapping, False)

                updated = datetime.fromisoformat(mapping.updated_at)
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age < stale_recovery_seconds:
                    db.commit()
                    return CaptureResult(mapping, False)
                claim_cursor = db.execute(
                    """
                    UPDATE bridge_jobs
                    SET owner_instance_id = ?, updated_at = ?
                    WHERE idempotency_key = ? AND owner_instance_id = ? AND updated_at = ?
                    """,
                    (
                        owner_instance_id,
                        now,
                        request.idempotency_key,
                        mapping.owner_instance_id,
                        mapping.updated_at,
                    ),
                )
                claimed = claim_cursor.rowcount == 1
                row = db.execute(
                    "SELECT * FROM bridge_jobs WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                db.commit()
                return CaptureResult(self._mapping(row), claimed, recovered=claimed)

    @staticmethod
    def _reply_mapping(row: sqlite3.Row) -> BridgeReplyMapping:
        return BridgeReplyMapping(
            reply_id=row["reply_id"],
            hermes_job_id=row["hermes_job_id"],
            prompt_id=row["prompt_id"],
            idempotency_key=row["idempotency_key"],
            origin=json.loads(row["origin_json"]),
            answer=row["answer"],
            phase=row["phase"],
            final_result=row["final_result"],
            owner_instance_id=row["owner_instance_id"],
            updated_at=row["updated_at"],
        )

    def create_pending_question(
        self, job_id: str, question: str, origin: BridgeOrigin
    ) -> PendingQuestion:
        prompt_id = f"prompt_{uuid.uuid4().hex}"
        now = _utc_now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE bridge_pending_questions
                SET status = 'superseded'
                WHERE hermes_job_id = ? AND status = 'pending'
                """,
                (job_id,),
            )
            db.execute(
                """
                INSERT INTO bridge_pending_questions (
                    prompt_id, hermes_job_id, question, origin_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    prompt_id,
                    job_id,
                    question,
                    json.dumps(origin.as_dict(), sort_keys=True),
                    now,
                ),
            )
        return PendingQuestion(prompt_id, job_id, question, origin.as_dict(), "pending")

    def get_pending_question(self, prompt_id: str) -> PendingQuestion | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_pending_questions WHERE prompt_id = ?",
                (prompt_id,),
            ).fetchone()
        if row is None:
            return None
        return PendingQuestion(
            prompt_id=row["prompt_id"],
            hermes_job_id=row["hermes_job_id"],
            question=row["question"],
            origin=json.loads(row["origin_json"]),
            status=row["status"],
        )

    def get_latest_pending_question(self, job_id: str) -> PendingQuestion | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM bridge_pending_questions
                WHERE hermes_job_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return PendingQuestion(
            prompt_id=row["prompt_id"],
            hermes_job_id=row["hermes_job_id"],
            question=row["question"],
            origin=json.loads(row["origin_json"]),
            status=row["status"],
        )

    def capture_reply(
        self,
        reply: BridgeReply,
        *,
        owner_instance_id: str,
        stale_recovery_seconds: int,
    ) -> ReplyCaptureResult:
        now = _utc_now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM bridge_replies WHERE idempotency_key = ?",
                (reply.idempotency_key,),
            ).fetchone()
            if existing is not None:
                mapping = self._reply_mapping(existing)
                if (
                    mapping.prompt_id != reply.prompt_id
                    or mapping.origin != reply.origin.as_dict()
                    or mapping.answer != reply.answer
                ):
                    db.rollback()
                    raise ValueError(
                        "Codex bridge reply idempotency key conflicts with another reply"
                    )
                job_row = db.execute(
                    "SELECT * FROM bridge_jobs WHERE hermes_job_id = ?",
                    (mapping.hermes_job_id,),
                ).fetchone()
                if (
                    mapping.phase in TERMINAL_PHASES
                    or mapping.owner_instance_id == owner_instance_id
                ):
                    db.commit()
                    return ReplyCaptureResult(mapping, self._mapping(job_row), False)
                updated = datetime.fromisoformat(mapping.updated_at)
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age < stale_recovery_seconds:
                    db.commit()
                    return ReplyCaptureResult(mapping, self._mapping(job_row), False)
                claimed = db.execute(
                    """
                    UPDATE bridge_replies SET owner_instance_id = ?, updated_at = ?
                    WHERE idempotency_key = ? AND owner_instance_id = ? AND updated_at = ?
                    """,
                    (
                        owner_instance_id,
                        now,
                        reply.idempotency_key,
                        mapping.owner_instance_id,
                        mapping.updated_at,
                    ),
                ).rowcount == 1
                row = db.execute(
                    "SELECT * FROM bridge_replies WHERE idempotency_key = ?",
                    (reply.idempotency_key,),
                ).fetchone()
                db.commit()
                return ReplyCaptureResult(
                    self._reply_mapping(row), self._mapping(job_row), claimed, claimed
                )

            question = db.execute(
                "SELECT * FROM bridge_pending_questions WHERE prompt_id = ?",
                (reply.prompt_id,),
            ).fetchone()
            if question is None:
                db.rollback()
                raise ValueError("Codex bridge prompt_id was not found")
            if question["status"] != "pending":
                db.rollback()
                raise ValueError("Codex bridge prompt has already been answered")
            stored_origin = json.loads(question["origin_json"])
            _validate_reply_origin(stored_origin, reply.origin)
            job_row = db.execute(
                "SELECT * FROM bridge_jobs WHERE hermes_job_id = ?",
                (question["hermes_job_id"],),
            ).fetchone()
            if job_row is None or job_row["phase"] != "needs_user":
                db.rollback()
                raise ValueError("Codex bridge job is not waiting for user input")
            if not job_row["codex_thread_id"]:
                db.rollback()
                raise ValueError("Codex bridge cannot resume without a persisted thread")

            reply_id = f"reply_{uuid.uuid4().hex}"
            db.execute(
                """
                INSERT INTO bridge_replies (
                    reply_id, hermes_job_id, prompt_id, idempotency_key,
                    origin_json, answer, phase, owner_instance_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'captured', ?, ?, ?)
                """,
                (
                    reply_id,
                    question["hermes_job_id"],
                    reply.prompt_id,
                    reply.idempotency_key,
                    json.dumps(reply.origin.as_dict(), sort_keys=True),
                    reply.answer,
                    owner_instance_id,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE bridge_pending_questions
                SET status = 'answered', reply_id = ?, answered_at = ?
                WHERE prompt_id = ? AND status = 'pending'
                """,
                (reply_id, now, reply.prompt_id),
            )
            row = db.execute(
                "SELECT * FROM bridge_replies WHERE reply_id = ?", (reply_id,)
            ).fetchone()
            db.commit()
            return ReplyCaptureResult(
                self._reply_mapping(row), self._mapping(job_row), True
            )

    def update_reply(
        self, reply_id: str, phase: str, final_result: str | None = None
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE bridge_replies
                SET phase = ?, final_result = COALESCE(?, final_result), updated_at = ?
                WHERE reply_id = ?
                """,
                (phase, final_result, _utc_now(), reply_id),
            )

    def set_thread_id(self, job_id: str, thread_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE bridge_jobs SET codex_thread_id = ?, updated_at = ? "
                "WHERE hermes_job_id = ?",
                (thread_id, _utc_now(), job_id),
            )

    def append_event(
        self,
        event: ProgressEvent,
        final_result: str | None = None,
        artifacts: tuple[str, ...] | None = None,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT INTO bridge_events (
                    event_id, hermes_job_id, phase, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.phase,
                    json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True),
                    event.created_at,
                ),
            )
            db.execute(
                """
                UPDATE bridge_jobs
                SET phase = ?, final_result = COALESCE(?, final_result),
                    artifacts_json = COALESCE(?, artifacts_json), updated_at = ?
                WHERE hermes_job_id = ?
                """,
                (
                    event.phase,
                    final_result,
                    json.dumps(artifacts, ensure_ascii=False) if artifacts is not None else None,
                    event.created_at,
                    event.task_id,
                ),
            )

    def get_by_idempotency(self, key: str) -> BridgeMapping | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._mapping(row) if row is not None else None

    def get_by_job_id(self, job_id: str) -> BridgeMapping | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM bridge_jobs WHERE hermes_job_id = ?", (job_id,)
            ).fetchone()
        return self._mapping(row) if row is not None else None

    def list_events(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM bridge_events "
                "WHERE hermes_job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
