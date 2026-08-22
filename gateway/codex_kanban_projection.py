"""Best-effort Kanban read model for the durable Codex bridge event stream.

The bridge database is authoritative.  This consumer may lag or fail without
changing Codex execution state, and it can rebuild its Kanban projection from
unreceipted public events after a restart or outage.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import mimetypes
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

_NOTIFICATION_PHASES = frozenset({"captured", "needs_user", "output_ready", "failed"})


@dataclass(frozen=True)
class KanbanProjectionSettings:
    """Fail-closed non-secret settings from ``config.yaml``."""

    enabled: bool = False
    board: str = "default"
    stale_claim_seconds: int = 60
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0

    @classmethod
    def from_mapping(cls, value: Any) -> "KanbanProjectionSettings":
        data = value if isinstance(value, Mapping) else {}
        raw_board = str(data.get("board", "default")).strip().lower()
        board = raw_board if raw_board else "default"
        if not all(ch.isalnum() or ch in "-_" for ch in board) or len(board) > 64:
            return cls()
        try:
            stale_claim_seconds = max(5, int(data.get("stale_claim_seconds", 60)))
        except (TypeError, ValueError):
            stale_claim_seconds = 60
        try:
            retry_initial_seconds = max(
                0.05, float(data.get("retry_initial_seconds", 1.0))
            )
            retry_max_seconds = max(
                retry_initial_seconds, float(data.get("retry_max_seconds", 30.0))
            )
            shutdown_timeout_seconds = max(
                0.1, float(data.get("shutdown_timeout_seconds", 5.0))
            )
        except (TypeError, ValueError):
            retry_initial_seconds = 1.0
            retry_max_seconds = 30.0
            shutdown_timeout_seconds = 5.0
        return cls(
            enabled=data.get("enabled") is True,
            board=board,
            stale_claim_seconds=stale_claim_seconds,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )


def load_kanban_projection_settings(
    config_path: Path | None = None,
) -> KanbanProjectionSettings:
    """Load the opt-in flag without importing or opening Kanban."""

    path = config_path or (get_hermes_home() / "config.yaml")
    if not path.exists():
        return KanbanProjectionSettings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not load Kanban projection config from %s: %s", path, exc)
        return KanbanProjectionSettings()
    return KanbanProjectionSettings.from_mapping(data.get("kanban_projection"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unix_time(value: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return int(time.time())


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_document(value: Any, limit: int) -> str:
    """Keep useful result formatting while enforcing the projection budget."""

    return str(value or "").strip()[:limit]


PROJECTION_DEPENDENCY_CONTRACT = {
    "name": "hermes-kanban-outcome-first",
    "version": 1,
    "required_api": {
        "connect": (),
        "create_task": ("initial_status", "idempotency_key"),
        "get_task": (),
        "write_txn": (),
        "publish_task_output": ("summary", "metadata", "with_reason"),
        "complete_task": ("result", "summary", "metadata", "with_reason"),
        "list_attachments": (),
        "store_attachment_bytes": ("uploaded_by", "board", "max_bytes"),
    },
    "required_task_columns": (
        "result",
        "current_step",
        "progress_percent",
        "latest_log",
        "files_changed",
        "progress_updated_at",
        "block_kind",
    ),
    "required_statuses": ("working", "output_ready"),
}


def probe_projection_dependency(
    api: Any,
    *,
    kanban_db_path: Path | None = None,
) -> dict[str, Any]:
    """Verify the outcome-first prerequisite without mutating Kanban state."""

    missing_api: list[str] = []
    incompatible_api: list[str] = []
    for name, required_parameters in PROJECTION_DEPENDENCY_CONTRACT["required_api"].items():
        function = getattr(api, name, None)
        if not callable(function):
            missing_api.append(name)
            continue
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            incompatible_api.append(f"{name}:signature-unavailable")
            continue
        missing_parameters = sorted(set(required_parameters) - set(parameters))
        if missing_parameters:
            incompatible_api.append(f"{name}:missing-{','.join(missing_parameters)}")

    valid_statuses = set(
        getattr(api, "VALID_STATUSES", getattr(api, "VALID_TASK_STATUSES", ()))
    )
    missing_statuses = sorted(
        set(PROJECTION_DEPENDENCY_CONTRACT["required_statuses"]) - valid_statuses
    )
    missing_columns: list[str] = []
    database_error: str | None = None
    if kanban_db_path is not None:
        path = Path(kanban_db_path)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
                }
            finally:
                connection.close()
            missing_columns = sorted(
                set(PROJECTION_DEPENDENCY_CONTRACT["required_task_columns"]) - columns
            )
        except (OSError, sqlite3.Error) as exc:
            database_error = f"{type(exc).__name__}: {exc}"[:500]

    blockers = bool(
        missing_api
        or incompatible_api
        or missing_statuses
        or missing_columns
        or database_error
    )
    return {
        "contract": PROJECTION_DEPENDENCY_CONTRACT["name"],
        "contract_version": PROJECTION_DEPENDENCY_CONTRACT["version"],
        "ready": not blockers,
        "missing_api": sorted(missing_api),
        "incompatible_api": sorted(incompatible_api),
        "missing_statuses": missing_statuses,
        "missing_task_columns": missing_columns,
        "database_error": database_error,
    }


class CodexKanbanProjector:
    """Pull public bridge events into an idempotent Kanban projection."""

    def __init__(
        self,
        bridge_db_path: Path,
        settings: KanbanProjectionSettings,
        *,
        kanban_db_path: Path | None = None,
        kanban_api: Any | None = None,
        owner_id: str | None = None,
    ):
        if not settings.enabled:
            raise ValueError("Kanban projection must be explicitly enabled")
        self.bridge_db_path = Path(bridge_db_path)
        self.settings = settings
        self.kanban_db_path = Path(kanban_db_path) if kanban_db_path else None
        self._kanban_api = kanban_api
        self.owner_id = owner_id or f"projection-{uuid.uuid4().hex}"
        self._lock = threading.Lock()
        self._initialize()

    def _source(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.bridge_db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._source() as db:
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

    def _kanban(self) -> Any:
        if self._kanban_api is None:
            from hermes_cli import kanban_db

            self._kanban_api = kanban_db
        return self._kanban_api

    def _connect_kanban(self, api: Any) -> sqlite3.Connection:
        if self.kanban_db_path is not None:
            return api.connect(db_path=self.kanban_db_path)
        return api.connect(board=self.settings.board)

    def dependency_status(self) -> dict[str, Any]:
        api = self._kanban()
        path = self.kanban_db_path
        if path is None:
            path = Path(api.kanban_db_path(board=self.settings.board))
        return probe_projection_dependency(api, kanban_db_path=path)

    def _pending(self, db: sqlite3.Connection) -> list[sqlite3.Row]:
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

    def _claim_job(self, job_id: str) -> bool:
        now = int(time.time())
        expires = now + self.settings.stale_claim_seconds
        with self._source() as db:
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

    def _mapped_task_id(self, job_id: str) -> str | None:
        with self._source() as db:
            row = db.execute(
                "SELECT kanban_task_id FROM bridge_projection_jobs "
                "WHERE hermes_job_id = ?",
                (job_id,),
            ).fetchone()
        return str(row["kanban_task_id"]) if row and row["kanban_task_id"] else None

    def _store_mapping(self, job_id: str, task_id: str) -> None:
        with self._source() as db:
            db.execute(
                """
                UPDATE bridge_projection_jobs
                SET kanban_task_id = ?, last_error = NULL, updated_at = ?
                WHERE hermes_job_id = ? AND claim_owner = ?
                """,
                (task_id, _utc_now(), job_id, self.owner_id),
            )

    def _ensure_card(
        self,
        api: Any,
        target: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> str:
        job_id = str(row["hermes_job_id"])
        task_id = self._mapped_task_id(job_id)
        if task_id and api.get_task(target, task_id) is not None:
            return task_id
        origin = json.loads(row["origin_json"] or "{}")
        origin_type = _bounded(origin.get("type"), 40) or "unknown"
        conversation = _bounded(origin.get("conversation_id"), 160) or "unknown"
        body = (
            "Executor: Codex\n"
            f"Origin: {origin_type}/{conversation}\n"
            f"Workspace: {row['workspace']}\n"
            "Source of truth: Codex Bridge event stream"
        )
        task_id = api.create_task(
            target,
            title=f"Codex task {job_id}",
            body=body,
            created_by="codex-bridge-projection",
            workspace_kind="dir",
            workspace_path=str(row["workspace"]),
            idempotency_key=f"codex-bridge:{job_id}",
            initial_status="working",
            board=self.settings.board,
        )
        self._store_mapping(job_id, task_id)
        return task_id

    @staticmethod
    def _task_row(target: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = target.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Projected Kanban card disappeared: {task_id}")
        return row

    @staticmethod
    def _require_outcome_schema(target: sqlite3.Connection) -> None:
        columns = {row["name"] for row in target.execute("PRAGMA table_info(tasks)")}
        required = {
            "current_step",
            "progress_percent",
            "latest_log",
            "files_changed",
            "progress_updated_at",
            "block_kind",
        }
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(
                "Kanban outcome-first schema is unavailable: " + ", ".join(missing)
            )

    @staticmethod
    def _require_dependency_api(api: Any) -> None:
        report = probe_projection_dependency(api)
        if not report["ready"]:
            details = {
                key: value
                for key, value in report.items()
                if key not in {"ready", "contract", "contract_version"} and value
            }
            raise RuntimeError(
                "Kanban projection dependency contract is unavailable: "
                + json.dumps(details, sort_keys=True)
            )

    def _set_live_state(
        self,
        api: Any,
        target: sqlite3.Connection,
        task_id: str,
        *,
        status: str,
        step: str,
        summary: str,
        timestamp: int,
        percent: int | None,
        block_kind: str | None = None,
    ) -> None:
        current = self._task_row(target, task_id)
        if current["status"] in {"output_ready", "review", "done", "archived"}:
            return
        with api.write_txn(target):
            target.execute(
                """
                UPDATE tasks
                SET status = ?, current_step = ?, progress_percent = COALESCE(?, progress_percent),
                    latest_log = ?, last_heartbeat_at = ?, progress_updated_at = ?,
                    block_kind = ?
                WHERE id = ? AND status NOT IN ('output_ready', 'review', 'done', 'archived')
                """,
                (
                    status,
                    step[:200],
                    percent,
                    summary[:800],
                    timestamp,
                    timestamp,
                    block_kind,
                    task_id,
                ),
            )

    def _project_event(
        self,
        api: Any,
        target: sqlite3.Connection,
        task_id: str,
        row: sqlite3.Row,
        event: Mapping[str, Any],
    ) -> None:
        phase = str(event.get("phase") or "")
        summary = _bounded(event.get("summary"), 500)
        progress = event.get("progress") if isinstance(event.get("progress"), Mapping) else {}
        step = _bounded(progress.get("current_step"), 180) or phase
        timestamp = _unix_time(event.get("created_at") or row["created_at"])
        if phase == "captured":
            self._set_live_state(
                api,
                target,
                task_id,
                status="working",
                step="Captured",
                summary=summary,
                timestamp=timestamp,
                percent=0,
            )
            return
        if phase == "working":
            self._set_live_state(
                api,
                target,
                task_id,
                status="working",
                step=step,
                summary=summary,
                timestamp=timestamp,
                percent=25,
            )
            return
        if phase == "needs_user":
            prompt_id = _bounded(progress.get("prompt_id"), 160)
            action = summary + (f" [prompt_id: {prompt_id}]" if prompt_id else "")
            self._set_live_state(
                api,
                target,
                task_id,
                status="blocked",
                step=f"Needs You: {summary}"[:200],
                summary=action,
                timestamp=timestamp,
                percent=None,
                block_kind="needs_input",
            )
            return
        if phase == "output_ready":
            current = self._task_row(target, task_id)
            final_result = _bounded_document(row["final_result"], 10_000) or summary
            artifacts = [
                _bounded(item, 500)
                for item in json.loads(row["artifacts_json"] or "[]")
                if _bounded(item, 500)
            ][:50]
            if current["status"] not in {"output_ready", "review", "done"}:
                result = api.publish_task_output(
                    target,
                    task_id,
                    summary=final_result,
                    metadata={
                        "executor": "codex",
                        "source_event_id": event.get("event_id"),
                        "artifacts": artifacts,
                    },
                    with_reason=True,
                )
                ok, reason = result if isinstance(result, tuple) else (bool(result), None)
                if not ok:
                    raise RuntimeError(reason or "Kanban rejected output-ready projection")
            self._attach_artifacts(
                api,
                target,
                task_id,
                row,
                event_id=str(event.get("event_id") or row["event_id"]),
                artifacts=artifacts,
            )
            with api.write_txn(target):
                target.execute(
                    """
                    UPDATE tasks
                    SET result = ?, files_changed = ?, current_step = 'Output ready',
                        progress_percent = 100, latest_log = ?, progress_updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        final_result,
                        json.dumps(artifacts, ensure_ascii=False),
                        summary,
                        timestamp,
                        task_id,
                    ),
                )
            return
        if phase == "done":
            current = self._task_row(target, task_id)
            if current["status"] == "done":
                return
            final_result = _bounded_document(row["final_result"], 10_000) or summary
            result = api.complete_task(
                target,
                task_id,
                result=final_result,
                summary=final_result,
                metadata={"executor": "codex", "source_event_id": event.get("event_id")},
                with_reason=True,
            )
            ok, reason = result if isinstance(result, tuple) else (bool(result), None)
            if not ok:
                raise RuntimeError(reason or "Kanban rejected done projection")
            return
        if phase == "failed":
            self._set_live_state(
                api,
                target,
                task_id,
                status="blocked",
                step="Execution failed",
                summary=summary,
                timestamp=timestamp,
                percent=None,
                block_kind="transient",
            )
            return
        raise RuntimeError(f"Unsupported bridge projection phase: {phase}")

    def _attach_artifacts(
        self,
        api: Any,
        target: sqlite3.Connection,
        task_id: str,
        row: sqlite3.Row,
        *,
        event_id: str,
        artifacts: list[str],
    ) -> None:
        """Mirror safe workspace artifacts into the existing one-click UI flow."""

        try:
            workspace = Path(str(row["workspace"])).resolve(strict=True)
        except OSError:
            return
        existing_markers = {
            str(item.uploaded_by)
            for item in api.list_attachments(target, task_id)
            if item.uploaded_by
        }
        max_bytes = int(getattr(api, "KANBAN_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024))
        for raw_path in artifacts:
            try:
                artifact = Path(raw_path).resolve(strict=True)
                artifact.relative_to(workspace)
                if not artifact.is_file() or artifact.stat().st_size > max_bytes:
                    continue
            except (OSError, ValueError):
                continue
            digest = hashlib.sha256(
                f"{event_id}\0{artifact}".encode("utf-8")
            ).hexdigest()[:24]
            marker = f"codex-bridge-projection:{digest}"
            if marker in existing_markers:
                continue
            try:
                api.store_attachment_bytes(
                    target,
                    task_id,
                    artifact.name,
                    artifact.read_bytes(),
                    content_type=mimetypes.guess_type(artifact.name)[0],
                    uploaded_by=marker,
                    board=self.settings.board,
                    max_bytes=max_bytes,
                )
                existing_markers.add(marker)
            except (OSError, ValueError):
                logger.warning(
                    "Could not mirror Codex artifact %s to Kanban card %s",
                    artifact,
                    task_id,
                    exc_info=True,
                )

    def _notification_eligible(self, db: sqlite3.Connection, row: sqlite3.Row) -> bool:
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

    def _record_success(self, row: sqlite3.Row, notification_eligible: bool) -> None:
        now = _utc_now()
        with self._source() as db:
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

    def _record_error(self, job_id: str, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {str(exc)}"[:500]
        with self._source() as db:
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
        with self._source() as db:
            job_ids = {
                str(row["hermes_job_id"]) for row in self._pending(db)
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

    def project_pending(self) -> int:
        """Drain unreceipted events; raise after recording a retryable failure."""

        projected = 0
        with self._lock:
            with self._source() as source:
                pending = self._pending(source)
            if not pending:
                return 0
            try:
                api = self._kanban()
                self._require_dependency_api(api)
                target = self._connect_kanban(api)
            except Exception as exc:
                for job_id in {str(row["hermes_job_id"]) for row in pending}:
                    self._record_error(job_id, exc)
                raise
            try:
                self._require_outcome_schema(target)
                for row in pending:
                    job_id = str(row["hermes_job_id"])
                    if not self._claim_job(job_id):
                        continue
                    try:
                        task_id = self._ensure_card(api, target, row)
                        event = json.loads(row["payload_json"])
                        self._project_event(api, target, task_id, row, event)
                        with self._source() as source:
                            notify = self._notification_eligible(source, row)
                        self._record_success(row, notify)
                        projected += 1
                    except Exception as exc:
                        self._record_error(job_id, exc)
                        raise
            except Exception as exc:
                for job_id in {str(row["hermes_job_id"]) for row in pending}:
                    self._record_error(job_id, exc)
                raise
            finally:
                target.close()
        return projected

    def status(self) -> dict[str, Any]:
        """Return operator-readable projection lag, cursor, error, and retry state."""

        with self._source() as db:
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
            "dependency": self.dependency_status(),
        }

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        with self._source() as db:
            row = db.execute(
                "SELECT * FROM bridge_projection_jobs WHERE hermes_job_id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_receipts(self, job_id: str) -> list[dict[str, Any]]:
        with self._source() as db:
            rows = db.execute(
                "SELECT * FROM bridge_projection_receipts "
                "WHERE hermes_job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def read_projection_status(
    bridge_db_path: Path,
    *,
    board: str = "default",
    kanban_db_path: Path | None = None,
    kanban_api: Any | None = None,
) -> dict[str, Any]:
    """Read projection/operator health without mutating either database."""

    source = sqlite3.connect(f"file:{Path(bridge_db_path)}?mode=ro", uri=True, timeout=5)
    source.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "bridge_projection_queue",
            "bridge_projection_jobs",
            "bridge_projection_receipts",
        }
        if not required.issubset(tables):
            pending_count = int(
                source.execute("SELECT count(1) FROM bridge_events").fetchone()[0]
            )
            cursor = 0
            receipt_count = 0
            job = None
        else:
            pending_count = int(
                source.execute(
                    """
                    SELECT count(1)
                    FROM bridge_projection_queue q
                    LEFT JOIN bridge_projection_receipts r ON r.event_id = q.event_id
                    WHERE r.event_id IS NULL
                    """
                ).fetchone()[0]
            )
            cursor = int(
                source.execute(
                    "SELECT COALESCE(MAX(projection_cursor), 0) FROM bridge_projection_jobs"
                ).fetchone()[0]
            )
            receipt_count = int(
                source.execute("SELECT count(1) FROM bridge_projection_receipts").fetchone()[0]
            )
            job_columns = {
                str(row["name"])
                for row in source.execute(
                    "PRAGMA table_info(bridge_projection_jobs)"
                ).fetchall()
            }
            retry_count_sql = (
                "retry_count" if "retry_count" in job_columns else "0 AS retry_count"
            )
            next_retry_sql = (
                "next_retry_at"
                if "next_retry_at" in job_columns
                else "NULL AS next_retry_at"
            )
            retry_state_sql = (
                "retry_state"
                if "retry_state" in job_columns
                else "'unknown' AS retry_state"
            )
            retry_filter = (
                " OR retry_state != 'idle'" if "retry_state" in job_columns else ""
            )
            job = source.execute(
                f"""
                SELECT last_error, {retry_count_sql}, {next_retry_sql},
                       {retry_state_sql}, updated_at
                FROM bridge_projection_jobs
                WHERE last_error IS NOT NULL{retry_filter}
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
    finally:
        source.close()

    if kanban_api is None:
        from hermes_cli import kanban_db as kanban_api

    target_path = kanban_db_path
    if target_path is None:
        target_path = Path(kanban_api.kanban_db_path(board=board))
    return {
        "mode": "read-only",
        "mutations": 0,
        "board": board,
        "pending_count": pending_count,
        "projection_cursor": cursor,
        "receipt_count": receipt_count,
        "last_error": job["last_error"] if job else None,
        "retry_count": int(job["retry_count"] or 0) if job else 0,
        "next_retry_at": job["next_retry_at"] if job else None,
        "retry_state": str(job["retry_state"] or "idle") if job else "idle",
        "updated_at": job["updated_at"] if job else None,
        "dependency": probe_projection_dependency(
            kanban_api, kanban_db_path=Path(target_path)
        ),
    }


class CodexKanbanReconciler:
    """Classify bridge/card drift without mutating either database."""

    def __init__(
        self,
        bridge_db_path: Path,
        *,
        board: str = "default",
        kanban_db_path: Path | None = None,
        kanban_api: Any | None = None,
    ):
        self.bridge_db_path = Path(bridge_db_path)
        self.board = board
        self.kanban_db_path = Path(kanban_db_path) if kanban_db_path else None
        self._kanban_api = kanban_api

    def _source(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.bridge_db_path}?mode=ro", uri=True, timeout=30
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _kanban(self) -> Any:
        if self._kanban_api is None:
            from hermes_cli import kanban_db

            self._kanban_api = kanban_db
        return self._kanban_api

    def inspect(self) -> dict[str, Any]:
        api = self._kanban()
        with self._source() as source:
            jobs = source.execute(
                "SELECT hermes_job_id, workspace, phase FROM bridge_jobs "
                "ORDER BY created_at, hermes_job_id"
            ).fetchall()
            projection_table = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='bridge_projection_jobs'"
            ).fetchone()
            mappings = {}
            if projection_table:
                mappings = {
                    row["hermes_job_id"]: row["kanban_task_id"]
                    for row in source.execute(
                        "SELECT hermes_job_id, kanban_task_id "
                        "FROM bridge_projection_jobs WHERE kanban_task_id IS NOT NULL"
                    ).fetchall()
                }
        target_path = (
            self.kanban_db_path
            if self.kanban_db_path is not None
            else Path(api.kanban_db_path(board=self.board))
        )
        target = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True, timeout=30)
        target.row_factory = sqlite3.Row
        try:
            tasks = api.list_tasks(target, include_archived=True)
        finally:
            target.close()

        source_ids = {str(row["hermes_job_id"]) for row in jobs}
        by_id = {task.id: task for task in tasks}
        by_key: dict[str, list[Any]] = {}
        for task in tasks:
            if task.idempotency_key:
                by_key.setdefault(str(task.idempotency_key), []).append(task)

        items: list[dict[str, Any]] = []
        for row in jobs:
            job_id = str(row["hermes_job_id"])
            expected_key = f"codex-bridge:{job_id}"
            exact = by_key.get(expected_key, [])
            if len(exact) > 1:
                classification = "duplicate_cards"
                task_ids = sorted(task.id for task in exact)
            elif len(exact) == 1:
                classification = "exact_match"
                task_ids = [exact[0].id]
            elif mappings.get(job_id) in by_id:
                classification = "mapped_match"
                task_ids = [str(mappings[job_id])]
            else:
                probable = [
                    task
                    for task in tasks
                    if job_id in str(task.title or "")
                    or (
                        task.workspace_path == row["workspace"]
                        and task.created_by == "codex-bridge-projection"
                    )
                ]
                if len(probable) == 1:
                    classification = "probable_legacy_match"
                    task_ids = [probable[0].id]
                elif len(probable) > 1:
                    classification = "ambiguous_legacy_match"
                    task_ids = sorted(task.id for task in probable)
                else:
                    classification = "missing_card"
                    task_ids = []
            items.append(
                {
                    "classification": classification,
                    "hermes_job_id": job_id,
                    "bridge_phase": row["phase"],
                    "workspace": row["workspace"],
                    "task_ids": task_ids,
                    "expected_idempotency_key": expected_key,
                }
            )

        for task in tasks:
            key = str(task.idempotency_key or "")
            if not key.startswith("codex-bridge:"):
                continue
            job_id = key.removeprefix("codex-bridge:")
            if job_id not in source_ids:
                items.append(
                    {
                        "classification": "orphan_card",
                        "hermes_job_id": job_id,
                        "bridge_phase": None,
                        "workspace": task.workspace_path,
                        "task_ids": [task.id],
                        "expected_idempotency_key": key,
                    }
                )

        counts: dict[str, int] = {}
        for item in items:
            classification = str(item["classification"])
            counts[classification] = counts.get(classification, 0) + 1
        return {
            "mode": "dry-run",
            "mutations": 0,
            "board": self.board,
            "bridge_jobs": len(jobs),
            "kanban_cards": len(tasks),
            "counts": dict(sorted(counts.items())),
            "items": items,
        }


def reconciliation_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Codex Bridge to Kanban reconciliation"
    )
    parser.add_argument(
        "--bridge-db",
        type=Path,
        default=get_hermes_home() / "codex_bridge" / "state.db",
    )
    parser.add_argument("--kanban-db", type=Path)
    parser.add_argument("--board", default="default")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--status",
        action="store_true",
        help="print read-only projection health instead of reconciliation",
    )
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        report = read_projection_status(
            args.bridge_db,
            board=args.board,
            kanban_db_path=args.kanban_db,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("Codex/Kanban projection status (read-only; 0 mutations)")
            print(f"Pending: {report['pending_count']}")
            print(f"Cursor: {report['projection_cursor']}")
            print(f"Retry: {report['retry_state']} ({report['retry_count']})")
            print(f"Last error: {report['last_error'] or '-'}")
            print(f"Dependency ready: {report['dependency']['ready']}")
        return 0 if report["dependency"]["ready"] else 2
    report = CodexKanbanReconciler(
        args.bridge_db,
        board=args.board,
        kanban_db_path=args.kanban_db,
    ).inspect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Codex/Kanban reconciliation (dry-run; 0 mutations)")
        print(f"Bridge jobs: {report['bridge_jobs']}")
        print(f"Kanban cards: {report['kanban_cards']}")
        for name, count in report["counts"].items():
            print(f"{name}: {count}")
    findings = sum(
        count
        for name, count in report["counts"].items()
        if name not in {"exact_match", "mapped_match"}
    )
    return 1 if args.fail_on_findings and findings else 0


if __name__ == "__main__":
    raise SystemExit(reconciliation_main())
