"""Idempotent SQLite migration for task budgets and kill-switch fences."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import (
    add_column_if_missing,
    open_connection,
    retrying_write_txn,
)


_DEFAULT_DB_PATH = get_default_hermes_root() / "kanban.db"
DB_PATH = _DEFAULT_DB_PATH
_MIGRATED_PATHS: set[str] = set()
_MIGRATION_LOCK = threading.RLock()

_KILL_SWITCH_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS task_kill_switch (
        task_id       TEXT PRIMARY KEY,
        killed_ts     TEXT NOT NULL,
        killed_by     TEXT NOT NULL,
        reason        TEXT NOT NULL
                      CHECK (reason IN (
                          'operator', 'per_task_cap', 'runaway', 'test'
                      )),
        notes         TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kill_switch_ts
        ON task_kill_switch(killed_ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_kill_switch_reason
        ON task_kill_switch(reason)
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"task caps ({path.name})",
    )


def migrate_connection(conn: sqlite3.Connection) -> None:
    """Apply the additive schema using an existing caller-owned connection."""
    for statement in _KILL_SWITCH_SCHEMA:
        conn.execute(statement)
    tasks_exists = conn.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type = 'table' AND name = 'tasks'
        """
    ).fetchone()
    if tasks_exists is None:
        return
    add_column_if_missing(
        conn,
        "tasks",
        "task_cap_aud",
        "task_cap_aud REAL",
    )
    add_column_if_missing(
        conn,
        "tasks",
        "failure_reason",
        "failure_reason TEXT",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_task_cap ON tasks(task_cap_aud)"
    )


def migrate(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            migrate_connection(conn)
    finally:
        conn.close()
    _MIGRATED_PATHS.add(str(path.resolve()))


def ensure_migrated(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    if key in _MIGRATED_PATHS:
        return
    with _MIGRATION_LOCK:
        if key not in _MIGRATED_PATHS:
            migrate(path)


__all__ = [
    "DB_PATH",
    "connect",
    "ensure_migrated",
    "migrate",
    "migrate_connection",
    "resolve_db_path",
]
