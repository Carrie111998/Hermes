"""Durable task kill fences and typed termination signals."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.cost import task_cap_schema
from hermes_cli.sqlite_util import retrying_write_txn


logger = logging.getLogger(__name__)
_REASONS = frozenset({"operator", "per_task_cap", "runaway", "test"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class KillSwitchTripped(Exception):
    def __init__(self, *, task_id: str, reason: str):
        self.task_id = str(task_id)
        self.reason = str(reason)
        super().__init__(
            f"task {self.task_id} is fenced by kill switch "
            f"(reason={self.reason})"
        )


class PerTaskCapExceeded(Exception):
    """Deprecated import-compatible signal from the former hard cost cap."""

    def __init__(
        self,
        *,
        task_id: str,
        current_total: float,
        projected_total: float,
        cap: float,
    ):
        self.task_id = str(task_id)
        self.current_total = float(current_total)
        self.projected_total = float(projected_total)
        self.cap = float(cap)
        super().__init__(
            f"task {self.task_id} projected AUD {self.projected_total:.6f} "
            f"exceeds cap AUD {self.cap:.6f} "
            f"(current AUD {self.current_total:.6f})"
        )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def is_task_killed(
    task_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return the durable fence row, or ``None`` when the task is live."""
    owns_connection = conn is None
    if owns_connection:
        task_cap_schema.ensure_migrated(db_path)
        conn = task_cap_schema.connect(db_path)
    assert conn is not None
    try:
        row = conn.execute(
            "SELECT * FROM task_kill_switch WHERE task_id = ?",
            (str(task_id),),
        ).fetchone()
        return _row_dict(row)
    finally:
        if owns_connection:
            conn.close()


def _insert_kill(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    killed_by: str,
    reason: str,
    notes: str | None,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO task_kill_switch (
            task_id, killed_ts, killed_by, reason, notes
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, _utc_now(), killed_by, reason, notes),
    )
    inserted = cursor.rowcount == 1
    if not inserted:
        logger.warning("Task %s is already killed; preserving first fence", task_id)
    return inserted


def kill_task(
    *,
    task_id: str,
    killed_by: str,
    reason: str,
    notes: str | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Insert the first durable fence; repeated kills preserve original audit."""
    normalized_task = str(task_id).strip()
    normalized_by = str(killed_by).strip()
    normalized_reason = str(reason).strip().lower()
    if not normalized_task:
        raise ValueError("task_id is required")
    if not normalized_by:
        raise ValueError("killed_by is required")
    if normalized_reason not in _REASONS:
        raise ValueError(
            f"invalid kill reason {reason!r}; expected one of {sorted(_REASONS)}"
        )
    if conn is not None:
        _insert_kill(
            conn,
            task_id=normalized_task,
            killed_by=normalized_by,
            reason=normalized_reason,
            notes=notes,
        )
        return

    task_cap_schema.ensure_migrated(db_path)
    owned = task_cap_schema.connect(db_path)
    try:
        with retrying_write_txn(owned):
            _insert_kill(
                owned,
                task_id=normalized_task,
                killed_by=normalized_by,
                reason=normalized_reason,
                notes=notes,
            )
    finally:
        owned.close()


def unkill_task(
    *,
    task_id: str,
    db_path: str | Path | None = None,
) -> None:
    """Remove only the fence; task status is deliberately untouched."""
    task_cap_schema.ensure_migrated(db_path)
    conn = task_cap_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                "DELETE FROM task_kill_switch WHERE task_id = ?",
                (str(task_id),),
            )
    finally:
        conn.close()


def list_killed_tasks(
    *,
    since_ts: str | None = None,
    lane: str | None = None,
    profile: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List fences with best-effort lane and task-profile attribution."""
    task_cap_schema.ensure_migrated(db_path)
    conn = task_cap_schema.connect(db_path)
    try:
        tasks_exists = (
            conn.execute(
                """
                SELECT 1
                  FROM sqlite_master
                 WHERE type = 'table' AND name = 'tasks'
                """
            ).fetchone()
            is not None
        )
        cost_exists = (
            conn.execute(
                """
                SELECT 1
                  FROM sqlite_master
                 WHERE type = 'table' AND name = 'cost_ledger'
                """
            ).fetchone()
            is not None
        )
        if profile is not None and not tasks_exists:
            return []
        where = []
        params: list[Any] = []
        if since_ts is not None:
            where.append("k.killed_ts >= ?")
            params.append(str(since_ts))
        if profile is not None:
            where.append("LOWER(COALESCE(t.assignee, '')) = ?")
            params.append(str(profile).strip().lower())
        if cost_exists and tasks_exists:
            lane_expr = """
                COALESCE(
                    (
                        SELECT c.lane
                          FROM cost_ledger AS c
                         WHERE c.task_id = k.task_id
                         ORDER BY c.id DESC
                         LIMIT 1
                    ),
                    t.assignee
                )
            """
        elif cost_exists:
            lane_expr = """
                (
                    SELECT c.lane
                      FROM cost_ledger AS c
                     WHERE c.task_id = k.task_id
                     ORDER BY c.id DESC
                     LIMIT 1
                )
            """
        elif tasks_exists:
            lane_expr = "t.assignee"
        else:
            lane_expr = "NULL"
        if lane is not None:
            if lane_expr == "NULL":
                return []
            where.append(f"LOWER(COALESCE({lane_expr}, '')) = ?")
            params.append(str(lane).strip().lower())
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        task_join = (
            "LEFT JOIN tasks AS t ON t.id = k.task_id"
            if tasks_exists
            else ""
        )
        profile_expr = "t.assignee" if tasks_exists else "NULL"
        params.append(max(1, int(limit)))
        rows = conn.execute(
            f"""
            SELECT k.task_id, k.killed_ts, k.killed_by, k.reason, k.notes,
                   {profile_expr} AS profile,
                   {lane_expr} AS lane
              FROM task_kill_switch AS k
              {task_join}
              {clause}
             ORDER BY k.killed_ts DESC, k.task_id
             LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "KillSwitchTripped",
    "PerTaskCapExceeded",
    "is_task_killed",
    "kill_task",
    "list_killed_tasks",
    "unkill_task",
]
