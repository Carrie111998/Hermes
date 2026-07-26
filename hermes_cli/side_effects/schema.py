"""Lazy, idempotent migration for the side-effect ledger."""

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

_SIDE_EFFECTS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS side_effects (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                  TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        task_id             TEXT,
        lane                TEXT NOT NULL
                            CHECK (lane IN (
                                'green_captains', 'dayroute', 'tihna',
                                'platform', 'reserve'
                            )),
        action_type         TEXT NOT NULL
                            CHECK (action_type IN (
                                'sms.send',
                                'retell.call',
                                'email.send',
                                'calendar.create',
                                'calendar.update',
                                'calendar.delete',
                                'gbp.post',
                                'gbp.reply',
                                'appstore.reply',
                                'github.pr.open',
                                'github.pr.comment',
                                'telegram.send',
                                'test.action'
                            )),
        payload_hash        TEXT NOT NULL,
        idempotency_key     TEXT NOT NULL,
        status              TEXT NOT NULL
                            CHECK (status IN (
                                'pending', 'in_flight', 'done',
                                'failed', 'stale', 'abandoned'
                            )),
        attempt_number      INTEGER NOT NULL DEFAULT 1,
        external_ref        TEXT,
        result_summary      TEXT,
        error_class         TEXT,
        error_message       TEXT,
        vendor              TEXT,
        allow_duplicate     BOOLEAN NOT NULL DEFAULT 0,
        UNIQUE (task_id, action_type, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_side_effects_lookup
        ON side_effects(action_type, payload_hash, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_side_effects_task
        ON side_effects(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_side_effects_ts
        ON side_effects(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_side_effects_stale
        ON side_effects(status, updated_at)
        WHERE status IN ('pending', 'in_flight')
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve an explicit override or the active shared Hermes root."""
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared store through the CS-01a hardened connection helper."""
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"side effects ({path.name})",
    )


def migrate(db_path: str | Path | None = None) -> None:
    """Create the side-effect table and indexes atomically."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            for statement in _SIDE_EFFECTS_SCHEMA:
                conn.execute(statement)
    finally:
        conn.close()
    _MIGRATED_PATHS.add(str(path.resolve()))


def ensure_migrated(db_path: str | Path | None = None) -> None:
    """Initialize each selected Hermes home at first use, once per process."""
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
    "resolve_db_path",
]
