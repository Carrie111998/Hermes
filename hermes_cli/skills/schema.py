"""Lazy SQLite schema for skill-lint audit records."""

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

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS skill_lint_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        skill_name    TEXT NOT NULL,
        skill_path    TEXT NOT NULL,
        write_source  TEXT,
        category      TEXT NOT NULL,
        pattern_label TEXT NOT NULL,
        matched_text  TEXT NOT NULL,
        line_number   INTEGER,
        replacement  TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_skill_lint_log_ts
        ON skill_lint_log(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_skill_lint_log_skill
        ON skill_lint_log(skill_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_skill_lint_log_category
        ON skill_lint_log(category)
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
        db_label=f"skill lint ({path.name})",
    )


def migrate(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
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


__all__ = ["DB_PATH", "connect", "ensure_migrated", "migrate", "resolve_db_path"]
