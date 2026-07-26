"""Lazy, shape-safe schema for the doctrine drift hourly rollup."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_cli.routing import schema as routing_schema
from hermes_cli.sqlite_util import open_connection, retrying_write_txn


_MIGRATED_PATHS: set[str] = set()
_MIGRATION_LOCK = threading.RLock()

BASE_COLUMNS = (
    "window_bucket_ts",
    "total_decisions",
    "followed_count",
    "overridden_count",
    "bypassed_count",
    "no_rule_count",
    "followed_pct",
    "overridden_pct",
    "bypassed_pct",
    "no_rule_pct",
    "top_override_lane",
    "top_override_count",
    "updated_ts",
)
EXPECTED_COLUMNS = BASE_COLUMNS + (
    "all_failed_count",
    "all_failed_pct",
)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS routing_drift_rollup (
        window_bucket_ts    TEXT PRIMARY KEY,
        total_decisions     INTEGER NOT NULL,
        followed_count      INTEGER NOT NULL,
        overridden_count    INTEGER NOT NULL,
        bypassed_count      INTEGER NOT NULL,
        no_rule_count       INTEGER NOT NULL,
        followed_pct        REAL NOT NULL,
        overridden_pct      REAL NOT NULL,
        bypassed_pct        REAL NOT NULL,
        no_rule_pct         REAL NOT NULL,
        top_override_lane   TEXT,
        top_override_count  INTEGER,
        updated_ts          TEXT NOT NULL,
        all_failed_count    INTEGER NOT NULL DEFAULT 0,
        all_failed_pct      REAL NOT NULL DEFAULT 0.0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_drift_rollup_updated
        ON routing_drift_rollup(updated_ts)
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    return routing_schema.resolve_db_path(db_path)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    return open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"routing drift ({path.name})",
    )


def _assert_compatible_existing_table(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type = 'table' AND name = 'routing_drift_rollup'
        """
    ).fetchone()
    if exists is None:
        return
    actual = tuple(
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(routing_drift_rollup)")
    )
    if actual not in {BASE_COLUMNS, EXPECTED_COLUMNS}:
        raise RuntimeError(
            "existing routing_drift_rollup table has an incompatible shape; "
            f"expected {EXPECTED_COLUMNS!r}, found {actual!r}"
        )


def migrate(db_path: str | Path | None = None) -> None:
    """Create the additive rollup table without touching decision rows."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            _assert_compatible_existing_table(conn)
            for statement in _SCHEMA:
                conn.execute(statement)
            actual = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(routing_drift_rollup)"
                )
            }
            if "all_failed_count" not in actual:
                conn.execute(
                    """
                    ALTER TABLE routing_drift_rollup
                    ADD COLUMN all_failed_count INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "all_failed_pct" not in actual:
                conn.execute(
                    """
                    ALTER TABLE routing_drift_rollup
                    ADD COLUMN all_failed_pct REAL NOT NULL DEFAULT 0.0
                    """
                )
            _assert_compatible_existing_table(conn)
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
    "EXPECTED_COLUMNS",
    "BASE_COLUMNS",
    "connect",
    "ensure_migrated",
    "migrate",
    "resolve_db_path",
]
