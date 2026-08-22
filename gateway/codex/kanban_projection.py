"""Outcome-first Kanban projection for public Codex bridge events."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from gateway.codex.kanban_contract import probe_projection_dependency
from gateway.codex.kanban_receipts import ProjectionReceiptStore
from gateway.codex.kanban_settings import (
    KanbanProjectionSettings,
    load_kanban_projection_settings,
)


logger = logging.getLogger("gateway.codex_kanban_projection")


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
        self.receipts = ProjectionReceiptStore(
            self.bridge_db_path, self.settings, self.owner_id
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

    def _ensure_card(
        self,
        api: Any,
        target: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> str:
        job_id = str(row["hermes_job_id"])
        task_id = self.receipts.mapped_task_id(job_id)
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
        self.receipts.store_mapping(job_id, task_id)
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

    def project_pending(self) -> int:
        """Drain unreceipted events; raise after recording a retryable failure."""

        projected = 0
        with self._lock:
            with self.receipts.source() as source:
                pending = self.receipts.pending(source)
            if not pending:
                return 0
            try:
                api = self._kanban()
                self._require_dependency_api(api)
                target = self._connect_kanban(api)
            except Exception as exc:
                for job_id in {str(row["hermes_job_id"]) for row in pending}:
                    self.receipts.record_error(job_id, exc)
                raise
            try:
                self._require_outcome_schema(target)
                for row in pending:
                    job_id = str(row["hermes_job_id"])
                    if not self.receipts.claim_job(job_id):
                        continue
                    try:
                        task_id = self._ensure_card(api, target, row)
                        event = json.loads(row["payload_json"])
                        self._project_event(api, target, task_id, row, event)
                        with self.receipts.source() as source:
                            notify = self.receipts.notification_eligible(source, row)
                        self.receipts.record_success(row, notify)
                        projected += 1
                    except Exception as exc:
                        self.receipts.record_error(job_id, exc)
                        raise
            except Exception as exc:
                for job_id in {str(row["hermes_job_id"]) for row in pending}:
                    self.receipts.record_error(job_id, exc)
                raise
            finally:
                target.close()
        return projected

    def record_retry_state(
        self, retry_count: int, next_retry_at: str | None, state: str
    ) -> None:
        self.receipts.record_retry_state(retry_count, next_retry_at, state)

    def status(self) -> dict[str, Any]:
        report = self.receipts.status()
        report["dependency"] = self.dependency_status()
        return report

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        return self.receipts.get_job_state(job_id)

    def list_receipts(self, job_id: str) -> list[dict[str, Any]]:
        return self.receipts.list_receipts(job_id)
