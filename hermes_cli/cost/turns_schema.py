"""Lazy SQLite schema for subscription turns and bridge health."""

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
    CREATE TABLE IF NOT EXISTS subscription_turns_ledger (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                TEXT NOT NULL,
        task_id           TEXT,
        lane              TEXT NOT NULL
                          CHECK (lane IN (
                              'green_captains', 'dayroute', 'tihna',
                              'platform', 'reserve', 'escalation'
                          )),
        vendor            TEXT NOT NULL DEFAULT 'openai-codex'
                          CHECK (vendor IN ('openai-codex')),
        bridge_tier       TEXT NOT NULL
                          CHECK (bridge_tier IN (
                              'pro', 'plus', 'free', 'unknown'
                          )),
        model_reported    TEXT,
        model_requested   TEXT,
        turns_consumed    INTEGER NOT NULL DEFAULT 1
                          CHECK (turns_consumed > 0),
        latency_ms        INTEGER,
        outcome           TEXT NOT NULL
                          CHECK (outcome IN (
                              'success', 'failure', 'degraded',
                              'rate_limited'
                          )),
        error_class       TEXT,
        error_message     TEXT,
        request_id        TEXT,
        raw_response_meta TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_ledger_ts
        ON subscription_turns_ledger(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_ledger_task
        ON subscription_turns_ledger(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_ledger_lane_ts
        ON subscription_turns_ledger(lane, ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_ledger_outcome
        ON subscription_turns_ledger(outcome)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_ledger_tier
        ON subscription_turns_ledger(bridge_tier)
    """,
    """
    CREATE TABLE IF NOT EXISTS bridge_health_log (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                 TEXT NOT NULL,
        source             TEXT NOT NULL
                           CHECK (source IN (
                               'probe', 'nightly', 'on_call', 'on_error'
                           )),
        outcome            TEXT NOT NULL
                           CHECK (outcome IN (
                               'healthy', 'degraded', 'exhausted', 'error'
                           )),
        tier_observed      TEXT,
        model_observed     TEXT,
        latency_ms         INTEGER,
        turns_used_today   INTEGER,
        turns_cap_daily    INTEGER,
        note               TEXT,
        raw                TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bridge_health_ts
        ON bridge_health_log(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bridge_health_source
        ON bridge_health_log(source)
    """,
    """
    CREATE TABLE IF NOT EXISTS bridge_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_ts TEXT NOT NULL
    )
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve an explicit override or the active shared Hermes database."""
    if db_path is not None:
        return Path(db_path).expanduser()
    configured = Path(DB_PATH).expanduser()
    if configured != _DEFAULT_DB_PATH:
        return configured
    return get_default_hermes_root() / "kanban.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared database through the CS-01a hardened helper."""
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"subscription bridge ({path.name})",
    )


def migrate(db_path: str | Path | None = None) -> None:
    """Create all bridge tables and indexes atomically and idempotently."""
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
    """Initialize each selected Hermes database once per process."""
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
