"""SQLite schema for audited coordinated service restarts."""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import open_connection, retrying_write_txn


_DEFAULT_DB_PATH = get_default_hermes_root() / "kanban.db"
DB_PATH = _DEFAULT_DB_PATH

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS service_manifest_state (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        version       INTEGER NOT NULL,
        applied_at    TEXT NOT NULL,
        applied_by    TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        is_active     INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_service_manifest_active
      ON service_manifest_state(is_active)
     WHERE is_active = 1
    """,
    """
    CREATE TABLE IF NOT EXISTS service_restart_run (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at               TEXT NOT NULL,
        ended_at                 TEXT,
        initiated_by             TEXT NOT NULL,
        reason                   TEXT,
        programme_state_at_start TEXT NOT NULL,
        overall_outcome          TEXT NOT NULL DEFAULT 'in_progress'
            CHECK (
                overall_outcome IN (
                    'in_progress', 'success', 'partial', 'failed', 'aborted'
                )
            )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_restart_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id              INTEGER NOT NULL
                                REFERENCES service_restart_run(id),
        service_id          TEXT NOT NULL,
        phase               TEXT NOT NULL
            CHECK (
                phase IN (
                    'drain', 'stop', 'start', 'health_check', 'skipped'
                )
            ),
        phase_started_at    TEXT NOT NULL,
        phase_ended_at      TEXT,
        outcome             TEXT NOT NULL
            CHECK (
                outcome IN (
                    'pending', 'success', 'failed', 'timeout', 'skipped'
                )
            ),
        old_pid             INTEGER,
        new_pid             INTEGER,
        health_check_output TEXT,
        error_repr          TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_service_restart_log_run
      ON service_restart_log(run_id)
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve an explicit test path or the shared default Kanban database."""
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: str | Path | None = None):
    """Open the restart audit database with the shared SQLite hardening."""
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"service restart ({path.name})",
    )


def ensure_migrated(db_path: str | Path | None = None) -> None:
    """Create the additive restart tables and indexes idempotently."""
    conn = connect(db_path)
    try:
        with retrying_write_txn(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
    finally:
        conn.close()


__all__ = ["DB_PATH", "connect", "ensure_migrated", "resolve_db_path"]
