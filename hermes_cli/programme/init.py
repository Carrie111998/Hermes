"""Persistence bootstrap for programme control.

Importing this module applies the idempotent migration to the active shared
Kanban database.  The shared root is deliberate: programme control spans
profiles, so Atlas, Mercury, Shield, and worker profiles must observe one row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import open_connection, retrying_write_txn


_DEFAULT_DB_PATH = get_default_hermes_root() / "kanban.db"
DB_PATH = _DEFAULT_DB_PATH

_PROGRAMME_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS programme_state (
        id                   INTEGER PRIMARY KEY CHECK (id = 1),
        state                TEXT NOT NULL
                             CHECK (state IN ('RUNNING', 'PAUSED', 'DRAINING', 'HALTED')),
        reason               TEXT,
        changed_by           TEXT,
        changed_at           TEXT NOT NULL,
        task_count_at_change INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS programme_state_log (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        state                TEXT NOT NULL,
        reason               TEXT,
        changed_by           TEXT,
        changed_at           TEXT NOT NULL,
        task_count_at_change INTEGER
    )
    """,
)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_db_path(db_path: Path | None = None) -> Path:
    """Resolve an explicit/test override or the active shared Hermes root."""
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: Path | None = None):
    """Open the programme database without starting an implicit transaction."""
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"programme ({path.name})",
    )


def migrate(db_path: Path | None = None) -> None:
    """Create programme tables and the singleton default row atomically."""
    conn = connect(db_path)
    try:
        with retrying_write_txn(conn):
            for statement in _PROGRAMME_SCHEMA:
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO programme_state (
                    id, state, reason, changed_by, changed_at, task_count_at_change
                )
                SELECT 1, 'RUNNING', 'initial', 'system', ?, 0
                 WHERE NOT EXISTS (SELECT 1 FROM programme_state WHERE id = 1)
                """,
                (utc_now(),),
            )
    finally:
        conn.close()


migrate()


__all__ = ["DB_PATH", "connect", "migrate", "resolve_db_path", "utc_now"]
