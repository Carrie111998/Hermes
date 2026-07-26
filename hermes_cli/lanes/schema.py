"""Additive, idempotent SQLite schema for the business-lane contract."""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import open_connection, retrying_write_txn

_DEFAULT_DB = get_default_hermes_root() / "kanban.db"

_DDL = (
    """CREATE TABLE IF NOT EXISTS lane_manifest_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL,
      applied_at TEXT NOT NULL, applied_by TEXT NOT NULL,
      manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
      is_active INTEGER NOT NULL DEFAULT 0)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ix_lane_manifest_active
       ON lane_manifest_state(is_active) WHERE is_active=1""",
    """CREATE TABLE IF NOT EXISTS lane_task (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
      external_id TEXT NOT NULL, task_id TEXT, ingested_at TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN (
        'ingested','drafting','drafted','awaiting_approval','publishing',
        'published','failed','expired')),
      payload_json TEXT NOT NULL, UNIQUE(lane_id,external_id))""",
    """CREATE INDEX IF NOT EXISTS ix_lane_task_status
       ON lane_task(lane_id,status)""",
    """CREATE TABLE IF NOT EXISTS lane_approval_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
      lane_task_id INTEGER NOT NULL REFERENCES lane_task(id),
      approval_token TEXT NOT NULL UNIQUE,
      channel TEXT NOT NULL CHECK(channel IN ('telegram','dashboard')),
      draft_json TEXT NOT NULL, created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','granted','rejected','expired')),
      grant_ts TEXT, grant_note TEXT, reject_reason TEXT)""",
    """CREATE INDEX IF NOT EXISTS ix_lane_approval_status
       ON lane_approval_queue(lane_id,status)""",
    """CREATE TABLE IF NOT EXISTS lane_publish_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
      lane_task_id INTEGER NOT NULL REFERENCES lane_task(id),
      approval_token TEXT NOT NULL, external_target TEXT NOT NULL,
      side_effect_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
      published_at TEXT NOT NULL,
      outcome TEXT NOT NULL CHECK(outcome IN (
        'success','failed','skipped_duplicate')), error_repr TEXT)""",
    """CREATE TABLE IF NOT EXISTS lane_rate_limit_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
      window_kind TEXT NOT NULL CHECK(window_kind IN (
        'hourly_ingest','daily_task','daily_cost')),
      window_start TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
      aud_total REAL NOT NULL DEFAULT 0.0,
      UNIQUE(lane_id,window_kind,window_start))""",
    """CREATE TABLE IF NOT EXISTS lane_metric (
      id INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
      lane_task_id INTEGER REFERENCES lane_task(id),
      metric_name TEXT NOT NULL, value REAL NOT NULL,
      recorded_at TEXT NOT NULL)""",
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path).expanduser() if db_path else _DEFAULT_DB


def connect(db_path: str | Path | None = None):
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"lanes ({path.name})",
    )


def ensure_migrated(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        with retrying_write_txn(conn):
            for statement in _DDL:
                conn.execute(statement)
    finally:
        conn.close()


__all__ = ["connect", "ensure_migrated", "resolve_db_path"]
