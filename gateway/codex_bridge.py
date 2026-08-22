"""Codex-first execution bridge for authenticated Hermes gateway requests.

The bridge is deliberately outside the Hermes agent loop: once a request is
captured here, Codex is its only executor.  Persisted events contain compact,
user-facing summaries only; SDK reasoning notifications are never stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import yaml

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

BRIDGE_PHASES = frozenset(
    {"captured", "working", "needs_user", "output_ready", "done", "failed"}
)
TERMINAL_PHASES = frozenset({"done", "failed"})
_DEFAULT_COMMAND_PREFIX = "/codex"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def request_fingerprint(prompt: str) -> str:
    """Return a non-reversible identity for idempotency collision checks."""

    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CodexBridgeSettings:
    """Non-secret bridge settings loaded from ``config.yaml``."""

    enabled: bool = False
    allowed_origins: tuple[str, ...] = ("local",)
    workspace_allowlist: tuple[str, ...] = ()
    default_workspace: str | None = None
    command_prefix: str = _DEFAULT_COMMAND_PREFIX
    model: str | None = None
    sandbox: str = "workspace-write"
    collaboration_mode: str = "default"
    stale_recovery_seconds: int = 60

    @classmethod
    def from_mapping(cls, value: Any) -> "CodexBridgeSettings":
        data = value if isinstance(value, Mapping) else {}
        raw_prefix = data.get("command_prefix", _DEFAULT_COMMAND_PREFIX)
        prefix = str(raw_prefix).strip() or _DEFAULT_COMMAND_PREFIX
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        sandbox = str(data.get("sandbox", "workspace-write")).strip().lower()
        if sandbox not in {"read-only", "workspace-write"}:
            sandbox = "workspace-write"
        collaboration_mode = str(
            data.get("collaboration_mode", "default")
        ).strip().lower()
        if collaboration_mode not in {"default", "plan"}:
            collaboration_mode = "default"
        try:
            stale_seconds = max(1, int(data.get("stale_recovery_seconds", 60)))
        except (TypeError, ValueError):
            stale_seconds = 60
        model = data.get("model")
        default_workspace = data.get("default_workspace")
        return cls(
            enabled=data.get("enabled") is True,
            allowed_origins=tuple(
                item.lower() for item in _coerce_string_list(data.get("allowed_origins"))
            )
            or ("local",),
            workspace_allowlist=_coerce_string_list(data.get("workspace_allowlist")),
            default_workspace=(
                str(default_workspace).strip() if default_workspace else None
            ),
            command_prefix=prefix,
            model=str(model).strip() if model else None,
            sandbox=sandbox,
            collaboration_mode=collaboration_mode,
            stale_recovery_seconds=stale_seconds,
        )


def load_codex_bridge_settings(config_path: Path | None = None) -> CodexBridgeSettings:
    """Load the feature flag without importing or starting the Codex SDK."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return CodexBridgeSettings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not load Codex bridge config from %s: %s", path, exc)
        return CodexBridgeSettings()
    return CodexBridgeSettings.from_mapping(data.get("codex_bridge"))


def legacy_workers_auto_dispatch_enabled(config_path: Path | None = None) -> bool:
    """Return the explicit legacy-worker gate; absence always means off."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    section = data.get("legacy_hermes_workers")
    return isinstance(section, Mapping) and section.get("auto_dispatch_enabled") is True


@dataclass(frozen=True)
class BridgeOrigin:
    type: str
    conversation_id: str
    message_id: str
    user_id: str | None = None
    thread_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "type": self.type,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
        }
        if self.user_id:
            result["user_id"] = self.user_id
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result


@dataclass(frozen=True)
class BridgeRequest:
    hermes_job_id: str
    idempotency_key: str
    origin: BridgeOrigin
    workspace: str
    prompt: str


@dataclass(frozen=True)
class BridgeReply:
    prompt_id: str
    idempotency_key: str
    origin: BridgeOrigin
    answer: str


@dataclass(frozen=True)
class ProgressEvent:
    event_id: str
    task_id: str
    executor: str
    phase: str
    summary: str
    origin: dict[str, str]
    created_at: str
    idempotency_key: str
    progress: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase not in BRIDGE_PHASES:
            raise ValueError(f"Unsupported bridge phase: {self.phase}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "executor": self.executor,
            "phase": self.phase,
            "summary": self.summary,
            "progress": self.progress,
            "origin": self.origin,
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class BridgeMapping:
    hermes_job_id: str
    idempotency_key: str
    request_fingerprint: str
    codex_thread_id: str | None
    origin: dict[str, str]
    workspace: str
    phase: str
    final_result: str | None
    artifacts: tuple[str, ...]
    owner_instance_id: str
    updated_at: str


@dataclass(frozen=True)
class CaptureResult:
    mapping: BridgeMapping
    should_execute: bool
    recovered: bool = False


@dataclass(frozen=True)
class PendingQuestion:
    prompt_id: str
    hermes_job_id: str
    question: str
    origin: dict[str, str]
    status: str


@dataclass(frozen=True)
class BridgeReplyMapping:
    reply_id: str
    hermes_job_id: str
    prompt_id: str
    idempotency_key: str
    origin: dict[str, str]
    answer: str
    phase: str
    final_result: str | None
    owner_instance_id: str
    updated_at: str


@dataclass(frozen=True)
class ReplyCaptureResult:
    mapping: BridgeReplyMapping
    job: BridgeMapping
    should_execute: bool
    recovered: bool = False


@dataclass(frozen=True)
class BridgeExecutionResult:
    final_response: str
    artifacts: tuple[str, ...] = ()


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


def validate_workspace(workspace: str, allowlist: tuple[str, ...]) -> Path:
    """Resolve a workspace and require it to be under an explicit allowlist."""

    if not workspace:
        raise ValueError("Codex bridge request is missing a workspace")
    if not allowlist:
        raise ValueError("codex_bridge.workspace_allowlist must contain at least one path")
    candidate = Path(workspace).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("Codex bridge workspace must be an existing directory")

    candidate_norm = os.path.normcase(str(candidate))
    for allowed in allowlist:
        try:
            root = Path(allowed).expanduser().resolve(strict=True)
            root_norm = os.path.normcase(str(root))
            if os.path.commonpath((candidate_norm, root_norm)) == root_norm:
                return candidate
        except (OSError, ValueError):
            continue
    raise ValueError("Codex bridge workspace is outside the configured allowlist")


def _validate_reply_origin(stored: Mapping[str, str], reply: BridgeOrigin) -> None:
    """Require a reply to come from the same channel conversation and user."""

    current = reply.as_dict()
    for key in ("type", "conversation_id", "user_id", "thread_id"):
        expected = stored.get(key)
        if expected and current.get(key) != expected:
            raise ValueError("Codex bridge reply origin does not match the pending request")


class CodexExecutor(Protocol):
    def execute(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        on_thread: Callable[[str], None],
        on_progress: Callable[[str, str], None],
    ) -> str | BridgeExecutionResult: ...


class CodexUserQuestion(RuntimeError):
    """A blocking structured question emitted by the Codex app server."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


_CODEX_USER_INPUT_METHODS = frozenset(
    {"item/tool/requestUserInput", "tool/requestUserInput"}
)


def _structured_codex_user_question(
    method: str, params: Mapping[str, Any] | None
) -> str | None:
    """Render a blocking Codex user-input request without parsing assistant text."""

    if method not in _CODEX_USER_INPUT_METHODS or not isinstance(params, Mapping):
        return None
    if params.get("isBlocking") is not True:
        return None
    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None

    rendered: list[str] = []
    for raw_question in raw_questions[:3]:
        if not isinstance(raw_question, Mapping):
            continue
        question = str(raw_question.get("question") or "").strip()
        if not question:
            continue
        header = str(raw_question.get("header") or "").strip()
        line = f"{header}: {question}" if header else question
        raw_options = raw_question.get("options")
        options: list[str] = []
        if isinstance(raw_options, list):
            for raw_option in raw_options[:3]:
                if not isinstance(raw_option, Mapping):
                    continue
                label = str(raw_option.get("label") or "").strip()
                description = str(raw_option.get("description") or "").strip()
                if label:
                    options.append(
                        f"{label} ({description})" if description else label
                    )
        if raw_question.get("isOther") is True:
            options.append("Câu trả lời khác")
        if options:
            line = f"{line}\nLựa chọn: {'; '.join(options)}"
        rendered.append(line)

    if not rendered:
        return None
    return "\n\n".join(rendered)[:4000]


def _unwrap_thread_item(item: Any) -> Any:
    return getattr(item, "root", item)


def _public_progress_for_item(item: Any) -> tuple[str, str] | None:
    """Map SDK item types to fixed summaries; never expose reasoning content."""

    item = _unwrap_thread_item(item)
    item_type = getattr(item, "type", None)
    if item_type == "commandExecution":
        return "execution", "Codex đang chạy và kiểm tra các lệnh trong workspace."
    if item_type == "fileChange":
        return "implementation", "Codex đã áp dụng một thay đổi tệp trong workspace."
    if item_type == "plan":
        return "planning", "Codex đã cập nhật kế hoạch thực thi."
    if item_type in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
        return "tooling", "Codex đang sử dụng một công cụ để tiếp tục task."
    return None


class CodexSdkExecutor:
    """Lazy Codex SDK adapter. Importing Hermes does not start app-server."""

    def __init__(self, settings: CodexBridgeSettings):
        self.settings = settings

    def execute(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        on_thread: Callable[[str], None],
        on_progress: Callable[[str, str], None],
    ) -> BridgeExecutionResult:
        try:
            from openai_codex import ApprovalMode, Codex, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "Codex bridge requires the 'codex-bridge' package extra"
            ) from exc

        sandbox = (
            Sandbox.read_only
            if self.settings.sandbox == "read-only"
            else Sandbox.workspace_write
        )
        def handle_server_request(
            method: str, params: Mapping[str, Any] | None
        ) -> dict[str, Any]:
            question = _structured_codex_user_question(method, params)
            if question:
                # Abort the SDK stream instead of supplying a fabricated
                # answer. Closing this app-server process stops the in-flight
                # turn; the durable bridge resumes the persisted thread only
                # after a correlated Hermes reply arrives.
                raise CodexUserQuestion(question)
            if method in _CODEX_USER_INPUT_METHODS:
                raise RuntimeError(
                    "Codex user-input request was non-blocking or invalid; "
                    "refusing to fabricate an answer"
                )
            return {}

        with Codex() as codex:
            sdk_client = getattr(codex, "_client", None)
            if sdk_client is None or not hasattr(sdk_client, "_approval_handler"):
                raise RuntimeError(
                    "Pinned Codex SDK does not expose server-request handling"
                )
            sdk_client._approval_handler = handle_server_request
            if codex_thread_id:
                thread = codex.thread_resume(
                    codex_thread_id,
                    cwd=request.workspace,
                    model=self.settings.model,
                    sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                )
            else:
                thread = codex.thread_start(
                    cwd=request.workspace,
                    model=self.settings.model,
                    sandbox=sandbox,
                    approval_mode=ApprovalMode.deny_all,
                    developer_instructions=(
                        "You are executing a Hermes-originated Codex task. Keep progress "
                        "user-facing, do not reveal private reasoning, and finish with a "
                        "concise result suitable for delivery to the origin channel."
                    ),
                )
            on_thread(thread.id)
            on_progress("codex_start", "Codex thread đã bắt đầu xử lý request.")

            if self.settings.collaboration_mode == "plan":
                from openai_codex.api import TurnHandle

                model = self.settings.model
                if not model:
                    models = codex.models().data
                    model = next(item.model for item in models if item.is_default)
                started = codex._client.turn_start(
                    thread.id,
                    request.prompt,
                    params={
                        "approvalPolicy": "never",
                        "cwd": request.workspace,
                        "sandboxPolicy": {
                            "type": (
                                "readOnly"
                                if self.settings.sandbox == "read-only"
                                else "workspaceWrite"
                            )
                        },
                        "collaborationMode": {
                            "mode": "plan",
                            "settings": {
                                "model": model,
                                "reasoning_effort": "low",
                                "developer_instructions": None,
                            },
                        },
                    },
                )
                handle = TurnHandle(codex._client, thread.id, started.turn.id)
            else:
                handle = thread.turn(request.prompt)
            final_response: str | None = None
            artifacts: set[str] = set()
            for notification in handle.stream():
                # Reasoning notifications and reasoning thread items are ignored.
                if notification.method not in {"item/started", "item/completed"}:
                    continue
                item = getattr(notification.payload, "item", None)
                public_progress = _public_progress_for_item(item)
                if public_progress and notification.method == "item/started":
                    on_progress(*public_progress)
                unwrapped = _unwrap_thread_item(item)
                if (
                    notification.method == "item/completed"
                    and getattr(unwrapped, "type", None) == "fileChange"
                ):
                    root = Path(request.workspace).resolve()
                    for change in getattr(unwrapped, "changes", ()):
                        raw_path = str(getattr(change, "path", "") or "").strip()
                        if not raw_path:
                            continue
                        candidate = Path(raw_path)
                        if not candidate.is_absolute():
                            candidate = root / candidate
                        try:
                            resolved = candidate.resolve()
                            root_norm = os.path.normcase(str(root))
                            resolved_norm = os.path.normcase(str(resolved))
                            if os.path.commonpath((root_norm, resolved_norm)) == root_norm:
                                artifacts.add(str(resolved))
                        except (OSError, ValueError):
                            continue
                if (
                    notification.method == "item/completed"
                    and getattr(unwrapped, "type", None) == "agentMessage"
                ):
                    phase = getattr(getattr(unwrapped, "phase", None), "value", None)
                    if phase == "final_answer" or phase == "finalAnswer":
                        final_response = getattr(unwrapped, "text", None)
                    elif phase is None:
                        final_response = getattr(unwrapped, "text", None)
            if not final_response:
                raise RuntimeError("Codex turn completed without a final response")
            return BridgeExecutionResult(final_response, tuple(sorted(artifacts)))


def _needs_user_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("login", "authentication", "authorization", "approval", "credential")
    )


class CodexBridgeService:
    def __init__(
        self,
        settings: CodexBridgeSettings,
        *,
        store: BridgeStore | None = None,
        executor: CodexExecutor | None = None,
        instance_id: str | None = None,
    ):
        self.settings = settings
        self.store = store or BridgeStore()
        self.executor = executor or CodexSdkExecutor(settings)
        self.instance_id = instance_id or uuid.uuid4().hex

    def _event(
        self,
        request: BridgeRequest,
        phase: str,
        summary: str,
        *,
        step: str,
        progress: Mapping[str, Any] | None = None,
    ) -> ProgressEvent:
        public_progress = {"current_step": step}
        if progress:
            public_progress.update(progress)
        return ProgressEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            task_id=request.hermes_job_id,
            executor="codex",
            phase=phase,
            summary=summary[:500],
            progress=public_progress,
            origin=request.origin.as_dict(),
            created_at=_utc_now(),
            idempotency_key=request.idempotency_key,
        )

    @staticmethod
    async def _notify(
        notify: Callable[[ProgressEvent], Any], event: ProgressEvent
    ) -> None:
        result = notify(event)
        if inspect.isawaitable(result):
            await result

    async def execute(
        self,
        request: BridgeRequest,
        notify: Callable[[ProgressEvent], Any],
    ) -> str:
        capture = self.store.capture(
            request,
            owner_instance_id=self.instance_id,
            stale_recovery_seconds=self.settings.stale_recovery_seconds,
        )
        if not capture.should_execute:
            if capture.mapping.final_result:
                return capture.mapping.final_result
            if capture.mapping.phase == "needs_user":
                pending = self.store.get_latest_pending_question(
                    capture.mapping.hermes_job_id
                )
                if pending:
                    return pending.question
            return "Request này đã được capture và đang được Codex xử lý."

        captured = self._event(
            request,
            "captured",
            "Hermes Gateway đã nhận request.",
            step="capture",
        )
        self.store.append_event(captured)
        await self._notify(notify, captured)

        working = self._event(
            request,
            "working",
            "Đang resume Codex thread đã persist."
            if capture.mapping.codex_thread_id
            else "Đang tạo Codex thread cho workspace đã được xác thực.",
            step="codex_start",
        )
        self.store.append_event(working)
        await self._notify(notify, working)

        try:
            outcome = await self._invoke_executor(
                request,
                codex_thread_id=capture.mapping.codex_thread_id,
                notify=notify,
            )
        except CodexUserQuestion as exc:
            return await self._record_needs_user(
                request, notify, question=exc.question
            )
        except Exception as exc:
            if _needs_user_error(exc):
                return await self._record_needs_user(request, notify)
            event = self._event(
                request,
                "failed",
                f"Codex execution failed: {type(exc).__name__}: {str(exc)[:240]}",
                step="failed",
            )
            self.store.append_event(event)
            await self._notify(notify, event)
            return event.summary

        final = outcome.final_response
        output_ready = self._event(
            request,
            "output_ready",
            "Codex đã hoàn tất và kết quả sẵn sàng để trả về origin.",
            step="delivery",
            progress={"artifacts": list(outcome.artifacts)},
        )
        self.store.append_event(
            output_ready, final_result=final, artifacts=outcome.artifacts
        )
        await self._notify(notify, output_ready)
        done = self._event(request, "done", "Kết quả đã được route về đúng origin.", step="done")
        self.store.append_event(done, final_result=final, artifacts=outcome.artifacts)
        return final

    async def _invoke_executor(
        self,
        request: BridgeRequest,
        *,
        codex_thread_id: str | None,
        notify: Callable[[ProgressEvent], Any],
    ) -> BridgeExecutionResult:
        loop = asyncio.get_running_loop()
        last_progress_step = "codex_start"
        emitted_action_updates = 0

        def on_thread(thread_id: str) -> None:
            self.store.set_thread_id(request.hermes_job_id, thread_id)

        def on_progress(step: str, summary: str) -> None:
            nonlocal emitted_action_updates, last_progress_step
            if step == last_progress_step:
                return
            # Phase 1 promises compact progress, not an unbounded tool trace.
            # Together with captured + initial working this caps ordinary
            # in-flight updates at four meaningful messages per execution.
            if emitted_action_updates >= 2:
                return
            last_progress_step = step
            emitted_action_updates += 1
            event = self._event(request, "working", summary, step=step)
            self.store.append_event(event)
            future = asyncio.run_coroutine_threadsafe(self._notify(notify, event), loop)
            future.result(timeout=30)

        result = await asyncio.to_thread(
            self.executor.execute,
            request,
            codex_thread_id=codex_thread_id,
            on_thread=on_thread,
            on_progress=on_progress,
        )
        if isinstance(result, BridgeExecutionResult):
            return result
        return BridgeExecutionResult(str(result))

    async def _record_needs_user(
        self,
        request: BridgeRequest,
        notify: Callable[[ProgressEvent], Any],
        *,
        reply_id: str | None = None,
        question: str | None = None,
    ) -> str:
        question = question or (
            "Codex cần đăng nhập hoặc quyền bổ sung trước khi có thể tiếp tục."
        )
        pending = self.store.create_pending_question(
            request.hermes_job_id, question, request.origin
        )
        event = self._event(
            request,
            "needs_user",
            question,
            step="user_action",
            progress={"prompt_id": pending.prompt_id},
        )
        self.store.append_event(event)
        if reply_id:
            self.store.update_reply(reply_id, "needs_user", question)
        await self._notify(notify, event)
        return event.summary

    async def resume_with_reply(
        self,
        reply: BridgeReply,
        notify: Callable[[ProgressEvent], Any],
    ) -> str:
        capture = self.store.capture_reply(
            reply,
            owner_instance_id=self.instance_id,
            stale_recovery_seconds=self.settings.stale_recovery_seconds,
        )
        if not capture.should_execute:
            if capture.mapping.final_result:
                return capture.mapping.final_result
            return "Reply này đã được capture và đang được Codex xử lý."

        request = BridgeRequest(
            hermes_job_id=capture.job.hermes_job_id,
            idempotency_key=reply.idempotency_key,
            origin=BridgeOrigin(**capture.mapping.origin),
            workspace=capture.job.workspace,
            prompt=capture.mapping.answer,
        )
        working = self._event(
            request,
            "working",
            "Hermes Gateway đã nhận reply và đang resume Codex thread đã persist.",
            step="codex_resume",
            progress={"prompt_id": reply.prompt_id},
        )
        self.store.append_event(working)
        self.store.update_reply(capture.mapping.reply_id, "working")
        await self._notify(notify, working)

        try:
            outcome = await self._invoke_executor(
                request,
                codex_thread_id=capture.job.codex_thread_id,
                notify=notify,
            )
        except CodexUserQuestion as exc:
            return await self._record_needs_user(
                request,
                notify,
                reply_id=capture.mapping.reply_id,
                question=exc.question,
            )
        except Exception as exc:
            if _needs_user_error(exc):
                return await self._record_needs_user(
                    request, notify, reply_id=capture.mapping.reply_id
                )
            event = self._event(
                request,
                "failed",
                f"Codex execution failed: {type(exc).__name__}: {str(exc)[:240]}",
                step="failed",
            )
            self.store.append_event(event)
            self.store.update_reply(capture.mapping.reply_id, "failed", event.summary)
            await self._notify(notify, event)
            return event.summary

        final = outcome.final_response
        output_ready = self._event(
            request,
            "output_ready",
            "Codex đã hoàn tất sau reply và kết quả sẵn sàng để trả về origin.",
            step="delivery",
            progress={"artifacts": list(outcome.artifacts)},
        )
        self.store.append_event(
            output_ready, final_result=final, artifacts=outcome.artifacts
        )
        await self._notify(notify, output_ready)
        done = self._event(
            request, "done", "Kết quả đã được route về đúng origin.", step="done"
        )
        self.store.append_event(done, final_result=final, artifacts=outcome.artifacts)
        self.store.update_reply(capture.mapping.reply_id, "done", final)
        return final


@dataclass(frozen=True)
class GatewayBridgeResult:
    handled: bool
    response: str | None = None


class GatewayCodexBridgeMixin:
    """Narrow GatewayRunner integration point for the opt-in bridge lane."""

    _codex_bridge_service: CodexBridgeService | None = None
    _codex_bridge_settings_cache: CodexBridgeSettings | None = None

    def _codex_bridge_settings(self) -> CodexBridgeSettings:
        cached = getattr(self, "_codex_bridge_settings_cache", None)
        if cached is None:
            cached = load_codex_bridge_settings()
            self._codex_bridge_settings_cache = cached
        return cached

    def _ensure_codex_bridge_service(
        self, settings: CodexBridgeSettings
    ) -> CodexBridgeService:
        if (
            self._codex_bridge_service is None
            or self._codex_bridge_service.settings != settings
        ):
            self._codex_bridge_service = CodexBridgeService(settings)
        return self._codex_bridge_service

    def _build_bridge_request(
        self, event: Any, settings: CodexBridgeSettings
    ) -> BridgeRequest | None:
        source = event.source
        origin_type = str(getattr(getattr(source, "platform", None), "value", "") or "")
        if not settings.enabled or origin_type not in settings.allowed_origins:
            return None

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        text = str(event.text or "")
        explicit = metadata.get("codex_bridge_request") is True
        prefix = settings.command_prefix
        if not explicit:
            if not text.lower().startswith(prefix.lower()):
                return None
            text = text[len(prefix) :].lstrip()
        if not text:
            raise ValueError("Codex bridge prompt is empty")

        message_id = str(event.message_id or metadata.get("source_message_id") or "").strip()
        chat_id = str(getattr(source, "chat_id", "") or "").strip()
        if not message_id or not chat_id:
            raise ValueError("Codex bridge origin requires message_id and conversation_id")
        workspace_raw = metadata.get("workspace") or settings.default_workspace
        workspace = validate_workspace(str(workspace_raw or ""), settings.workspace_allowlist)
        idempotency_key = str(
            metadata.get("idempotency_key")
            or f"{origin_type}:{chat_id}:{message_id}"
        )
        job_id = str(
            metadata.get("hermes_job_id")
            or "job_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        )
        return BridgeRequest(
            hermes_job_id=job_id,
            idempotency_key=idempotency_key,
            origin=BridgeOrigin(
                type=origin_type,
                conversation_id=chat_id,
                message_id=message_id,
                user_id=str(getattr(source, "user_id", "") or "") or None,
                thread_id=str(getattr(source, "thread_id", "") or "") or None,
            ),
            workspace=str(workspace),
            prompt=text,
        )

    def _build_bridge_reply(
        self, event: Any, settings: CodexBridgeSettings
    ) -> BridgeReply | None:
        source = event.source
        origin_type = str(getattr(getattr(source, "platform", None), "value", "") or "")
        if not settings.enabled or origin_type not in settings.allowed_origins:
            return None
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        prompt_id = str(metadata.get("codex_bridge_prompt_id") or "").strip()
        if not prompt_id:
            return None
        answer = str(event.text or "").strip()
        message_id = str(event.message_id or metadata.get("source_message_id") or "").strip()
        conversation_id = str(getattr(source, "chat_id", "") or "").strip()
        if not answer:
            raise ValueError("Codex bridge reply is empty")
        if not message_id or not conversation_id:
            raise ValueError("Codex bridge reply requires message_id and conversation_id")
        return BridgeReply(
            prompt_id=prompt_id,
            idempotency_key=str(
                metadata.get("idempotency_key")
                or f"{origin_type}:{conversation_id}:{message_id}"
            ),
            origin=BridgeOrigin(
                type=origin_type,
                conversation_id=conversation_id,
                message_id=message_id,
                user_id=str(getattr(source, "user_id", "") or "") or None,
                thread_id=str(getattr(source, "thread_id", "") or "") or None,
            ),
            answer=answer,
        )

    async def _maybe_handle_codex_bridge(
        self,
        event: Any,
        notify_override: Callable[[ProgressEvent], Any] | None = None,
    ) -> GatewayBridgeResult:
        settings = self._codex_bridge_settings()
        try:
            reply = self._build_bridge_reply(event, settings)
            request = None if reply else self._build_bridge_request(event, settings)
        except ValueError as exc:
            return GatewayBridgeResult(True, f"Codex bridge rejected request: {exc}")
        if request is None and reply is None:
            return GatewayBridgeResult(False)

        service = self._ensure_codex_bridge_service(settings)
        adapter = None if notify_override is not None else self._adapter_for_source(event.source)
        if notify_override is None and adapter is None:
            return GatewayBridgeResult(
                True, "Codex bridge origin adapter is unavailable."
            )

        origin = reply.origin if reply else request.origin

        async def notify(progress: ProgressEvent) -> None:
            # Final result is returned through the gateway's normal response path.
            # These are compact interim notices only.
            if notify_override is not None:
                result = notify_override(progress)
                if inspect.isawaitable(result):
                    await result
                return
            metadata: dict[str, Any] = {
                "_interim_send": True,
                "codex_bridge_event": progress.as_dict(),
            }
            if origin.thread_id:
                metadata["thread_id"] = origin.thread_id
            await adapter.send(
                origin.conversation_id,
                progress.summary,
                reply_to=origin.message_id,
                metadata=metadata,
            )

        try:
            response = (
                await service.resume_with_reply(reply, notify)
                if reply
                else await service.execute(request, notify)
            )
        except ValueError as exc:
            kind = "reply" if reply else "request"
            return GatewayBridgeResult(True, f"Codex bridge rejected {kind}: {exc}")
        return GatewayBridgeResult(True, response)
