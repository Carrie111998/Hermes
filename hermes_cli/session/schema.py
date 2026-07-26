"""Lazy, shape-safe migration for the shared session-rotation ledger."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import open_connection, retrying_write_txn


_DEFAULT_DB_PATH = get_default_hermes_root() / "kanban.db"
DB_PATH = _DEFAULT_DB_PATH
_MIGRATED_PATHS: set[str] = set()
_MIGRATION_LOCK = threading.RLock()

EXPECTED_COLUMNS = (
    "id",
    "task_id",
    "parent_session_id",
    "lane",
    "profile",
    "route",
    "opened_ts",
    "closed_ts",
    "rotation_reason",
    "token_count_at_close",
    "handoff_summary_json",
)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id                    TEXT PRIMARY KEY,
        task_id               TEXT NOT NULL,
        parent_session_id     TEXT,
        lane                  TEXT NOT NULL,
        profile               TEXT,
        route                 TEXT,
        opened_ts             TEXT NOT NULL,
        closed_ts             TEXT,
        rotation_reason       TEXT
                              CHECK (
                                  rotation_reason IS NULL OR rotation_reason IN (
                                      'soft_limit', 'hard_limit', 'manual', 'error'
                                  )
                              ),
        token_count_at_close  INTEGER,
        handoff_summary_json  TEXT,
        FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_opened ON sessions(opened_ts)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_closed ON sessions(closed_ts)",
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
        db_label=f"session rotation ({path.name})",
    )


def _assert_compatible_existing_table(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        """
        SELECT sql
          FROM sqlite_master
         WHERE type = 'table' AND name = 'sessions'
        """
    ).fetchone()
    if table is None:
        return
    actual = tuple(
        str(row["name"]) for row in conn.execute("PRAGMA table_info(sessions)")
    )
    if actual != EXPECTED_COLUMNS:
        raise RuntimeError(
            "existing sessions table has an incompatible shape; "
            f"expected {EXPECTED_COLUMNS!r}, found {actual!r}"
        )


def migrate(db_path: str | Path | None = None) -> None:
    """Create only the requested table when absent; never rebuild a conflict."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            _assert_compatible_existing_table(conn)
            for statement in _SCHEMA:
                conn.execute(statement)
    finally:
        conn.close()

    # These migrations own their respective tables and preserve all rows.
    from hermes_cli.cost import ledger
    from hermes_cli.verdict import schema as verdict_schema

    ledger.migrate(path)
    verdict_schema.migrate(path)
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
    "EXPECTED_COLUMNS",
    "connect",
    "ensure_migrated",
    "migrate",
    "resolve_db_path",
]
