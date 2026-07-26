"""Lazy, shape-safe schema for versioned routing doctrine and decisions."""

from __future__ import annotations

import os
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

EXPECTED_COLUMNS = {
    "routing_doctrine": (
        "id",
        "version",
        "lane",
        "rung",
        "complexity",
        "primary_provider",
        "primary_model",
        "fallback_chain_json",
        "forbid_paths_json",
        "notes",
        "priority",
        "created_ts",
    ),
    "routing_doctrine_meta": (
        "singleton",
        "active_version",
        "previous_version",
        "last_activated_ts",
        "last_activated_by",
    ),
    "routing_doctrine_activations": (
        "id",
        "activated_version",
        "deactivated_version",
        "activated_ts",
        "activated_by",
        "activation_type",
        "notes",
    ),
    "routing_decisions": (
        "id",
        "session_id",
        "task_id",
        "profile",
        "route",
        "lane",
        "rung",
        "complexity",
        "chosen_provider",
        "chosen_model",
        "doctrine_version",
        "matched_rule_id",
        "match_specificity",
        "used_doctrine_reader",
        "overridden_by_caller",
        "doctrine_suggested_provider",
        "doctrine_suggested_model",
        "failure_history_json",
        "chosen_at",
        "forced_legacy",
    ),
}

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS routing_doctrine (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        version               INTEGER NOT NULL,
        lane                  TEXT    NOT NULL,
        rung                  TEXT    NOT NULL,
        complexity            TEXT    NOT NULL,
        primary_provider      TEXT    NOT NULL,
        primary_model         TEXT    NOT NULL,
        fallback_chain_json   TEXT    NOT NULL DEFAULT '[]',
        forbid_paths_json     TEXT    NOT NULL DEFAULT '[]',
        notes                 TEXT,
        priority              INTEGER NOT NULL DEFAULT 0,
        created_ts            TEXT    NOT NULL,
        UNIQUE (
            version, lane, rung, complexity,
            primary_provider, primary_model
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doctrine_version
        ON routing_doctrine(version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_doctrine_specific
        ON routing_doctrine(version, lane, rung, complexity)
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_doctrine_meta (
        singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
        active_version         INTEGER NOT NULL,
        previous_version       INTEGER,
        last_activated_ts      TEXT NOT NULL,
        last_activated_by      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_doctrine_activations (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        activated_version      INTEGER NOT NULL,
        deactivated_version    INTEGER,
        activated_ts           TEXT NOT NULL,
        activated_by           TEXT NOT NULL,
        activation_type        TEXT NOT NULL
                               CHECK (
                                   activation_type IN (
                                       'bootstrap', 'activate', 'deactivate'
                                   )
                               ),
        notes                  TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activations_when
        ON routing_doctrine_activations(activated_ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activations_ver
        ON routing_doctrine_activations(activated_version)
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_decisions (
        id                              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id                      TEXT,
        task_id                         TEXT,
        profile                         TEXT,
        route                           TEXT,
        lane                            TEXT NOT NULL,
        rung                            TEXT NOT NULL,
        complexity                      TEXT NOT NULL,
        chosen_provider                 TEXT NOT NULL,
        chosen_model                    TEXT NOT NULL,
        doctrine_version                INTEGER,
        matched_rule_id                 INTEGER,
        match_specificity               TEXT,
        used_doctrine_reader            INTEGER NOT NULL
                                        CHECK (
                                            used_doctrine_reader IN (0, 1)
                                        ),
        overridden_by_caller            INTEGER NOT NULL DEFAULT 0
                                        CHECK (
                                            overridden_by_caller IN (0, 1)
                                        ),
        doctrine_suggested_provider     TEXT,
        doctrine_suggested_model        TEXT,
        failure_history_json            TEXT NOT NULL DEFAULT '[]',
        chosen_at                       TEXT NOT NULL,
        forced_legacy                   INTEGER NOT NULL DEFAULT 0
                                        CHECK (forced_legacy IN (0, 1)),
        FOREIGN KEY (matched_rule_id) REFERENCES routing_doctrine(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_session
        ON routing_decisions(session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_task
        ON routing_decisions(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_when
        ON routing_decisions(chosen_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_profile
        ON routing_decisions(profile)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_route
        ON routing_decisions(route)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_forced_legacy
        ON routing_decisions(forced_legacy)
    """,
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser()
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
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
        db_label=f"routing doctrine ({path.name})",
    )


def _assert_compatible_existing_tables(conn: sqlite3.Connection) -> None:
    for table, expected in EXPECTED_COLUMNS.items():
        exists = conn.execute(
            """
            SELECT 1
              FROM sqlite_master
             WHERE type = 'table' AND name = ?
            """,
            (table,),
        ).fetchone()
        if exists is None:
            continue
        actual = tuple(
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})")
        )
        if (
            table == "routing_decisions"
            and actual == expected[:-1]
        ):
            continue
        if actual != expected:
            raise RuntimeError(
                f"existing {table} table has an incompatible shape; "
                f"expected {expected!r}, found {actual!r}"
            )


def migrate(db_path: str | Path | None = None) -> None:
    """Create the doctrine tables without rebuilding a conflicting table."""
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            _assert_compatible_existing_tables(conn)
            decisions_exists = conn.execute(
                """
                SELECT 1
                  FROM sqlite_master
                 WHERE type = 'table' AND name = 'routing_decisions'
                """
            ).fetchone()
            if decisions_exists is not None:
                add_column_if_missing(
                    conn,
                    "routing_decisions",
                    "forced_legacy",
                    (
                        "forced_legacy INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (forced_legacy IN (0, 1))"
                    ),
                )
            for statement in _SCHEMA:
                conn.execute(statement)
            _assert_compatible_existing_tables(conn)
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
    "EXPECTED_COLUMNS",
    "connect",
    "ensure_migrated",
    "migrate",
    "resolve_db_path",
]
