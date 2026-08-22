"""Read-only projection status and Codex/Kanban drift classification."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from gateway.codex.kanban_contract import probe_projection_dependency


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
