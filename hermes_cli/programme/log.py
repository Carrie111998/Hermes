"""Append-only programme state history writer."""

from __future__ import annotations

import sqlite3


def append_state_log(
    conn: sqlite3.Connection,
    *,
    state: str,
    reason: str | None,
    changed_by: str | None,
    changed_at: str,
    task_count_at_change: int | None,
) -> int:
    """Append one immutable history row using the caller's transaction."""
    cursor = conn.execute(
        """
        INSERT INTO programme_state_log (
            state, reason, changed_by, changed_at, task_count_at_change
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (state, reason, changed_by, changed_at, task_count_at_change),
    )
    return int(cursor.lastrowid or 0)


__all__ = ["append_state_log"]
