"""Lazy SQLite schema for leaf verdicts and dispatch envelopes."""

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

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS leaf_verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        task_id TEXT NOT NULL,
        task_run_id INTEGER,
        attempt_number INTEGER NOT NULL,
        rung_id TEXT NOT NULL,
        dispatch_envelope_id INTEGER,
        model_used TEXT NOT NULL,
        outcome TEXT NOT NULL
            CHECK (outcome IN (
                'success', 'failure', 'partial', 'aborted',
                'killed_by_cap', 'killed_by_operator'
            )),
        failure_class TEXT
            CHECK (failure_class IS NULL OR failure_class IN (
                'infra', 'quality', 'capability', 'budget', 'ambiguous',
                'cost_cap', 'operator'
            )),
        failure_signals TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        cost_aud REAL NOT NULL DEFAULT 0.0,
        side_effects TEXT NOT NULL DEFAULT '[]',
        escalation_recommended INTEGER NOT NULL DEFAULT 0,
        recommendation_reason TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        wall_ms INTEGER,
        strategy_hash TEXT NOT NULL,
        error_class TEXT,
        error_message TEXT,
        raw_meta TEXT,
        profile TEXT,
        route TEXT,
        session_id TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_leaf_verdicts_task
        ON leaf_verdicts(task_id, attempt_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_leaf_verdicts_ts
        ON leaf_verdicts(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_leaf_verdicts_rung
        ON leaf_verdicts(rung_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_leaf_verdicts_failure_class
        ON leaf_verdicts(failure_class)
    """,
    """
    CREATE TABLE IF NOT EXISTS dispatch_envelopes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        task_id TEXT NOT NULL,
        task_run_id INTEGER,
        attempt_number INTEGER NOT NULL,
        rung_id TEXT NOT NULL,
        model_slug TEXT NOT NULL,
        mode TEXT NOT NULL
            CHECK (mode IN (
                'single', 'single_with_critic', 'moa', 'panel', 'decompose'
            )),
        strategy_hash TEXT NOT NULL,
        strategy_payload TEXT NOT NULL,
        parent_verdict_id INTEGER,
        expected_cost_aud REAL,
        issued_by TEXT,
        profile TEXT,
        route TEXT,
        session_id TEXT,
        FOREIGN KEY(parent_verdict_id) REFERENCES leaf_verdicts(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dispatch_envelopes_task
        ON dispatch_envelopes(task_id, attempt_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dispatch_envelopes_ts
        ON dispatch_envelopes(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dispatch_envelopes_rung
        ON dispatch_envelopes(rung_id)
    """,
)

_SESSION_INDEX_SCHEMA = (
    """
    CREATE INDEX IF NOT EXISTS idx_leaf_verdicts_session
        ON leaf_verdicts(session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dispatch_envelopes_session
        ON dispatch_envelopes(session_id)
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
        db_label=f"verdict ({path.name})",
    )


def _create_statement(table: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {table}"
    for statement in _SCHEMA:
        if marker in statement:
            return statement
    raise RuntimeError(f"missing schema statement for {table}")


def _copy_all_columns(
    conn: sqlite3.Connection,
    source: str,
    destination: str,
) -> None:
    columns = [
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({source})")
    ]
    quoted = ", ".join(f'"{column}"' for column in columns)
    conn.execute(
        f'INSERT INTO "{destination}" ({quoted}) '
        f'SELECT {quoted} FROM "{source}" ORDER BY id'
    )


def _leaf_constraints_need_cs10a(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT sql
          FROM sqlite_master
         WHERE type = 'table' AND name = 'leaf_verdicts'
        """
    ).fetchone()
    sql = str(row["sql"] if row is not None else "").lower()
    return "killed_by_cap" not in sql or "'cost_cap'" not in sql


def _rebuild_leaf_constraints(conn: sqlite3.Connection) -> None:
    """Widen verdict enums while preserving child dispatch foreign keys."""
    leaf_new = "leaf_verdicts__cs10a"
    dispatch_new = "dispatch_envelopes__cs10a"
    conn.execute(f"DROP TABLE IF EXISTS {dispatch_new}")
    conn.execute(f"DROP TABLE IF EXISTS {leaf_new}")

    leaf_sql = _create_statement("leaf_verdicts").replace(
        "CREATE TABLE IF NOT EXISTS leaf_verdicts",
        f"CREATE TABLE {leaf_new}",
        1,
    )
    conn.execute(leaf_sql)
    _copy_all_columns(conn, "leaf_verdicts", leaf_new)

    dispatch_sql = _create_statement("dispatch_envelopes").replace(
        "CREATE TABLE IF NOT EXISTS dispatch_envelopes",
        f"CREATE TABLE {dispatch_new}",
        1,
    ).replace(
        "REFERENCES leaf_verdicts(id)",
        f"REFERENCES {leaf_new}(id)",
        1,
    )
    conn.execute(dispatch_sql)
    _copy_all_columns(conn, "dispatch_envelopes", dispatch_new)

    # Drop child first so foreign-key enforcement remains valid throughout.
    conn.execute("DROP TABLE dispatch_envelopes")
    conn.execute("DROP TABLE leaf_verdicts")
    conn.execute(f"ALTER TABLE {leaf_new} RENAME TO leaf_verdicts")
    conn.execute(f"ALTER TABLE {dispatch_new} RENAME TO dispatch_envelopes")


def migrate(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    conn = connect(path)
    try:
        with retrying_write_txn(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
            if _leaf_constraints_need_cs10a(conn):
                _rebuild_leaf_constraints(conn)
                # Recreate indexes dropped with the two legacy tables.
                for statement in _SCHEMA:
                    conn.execute(statement)
            add_column_if_missing(
                conn,
                "leaf_verdicts",
                "profile",
                "profile TEXT",
            )
            add_column_if_missing(
                conn,
                "leaf_verdicts",
                "route",
                "route TEXT",
            )
            add_column_if_missing(
                conn,
                "leaf_verdicts",
                "session_id",
                "session_id TEXT",
            )
            add_column_if_missing(
                conn,
                "dispatch_envelopes",
                "profile",
                "profile TEXT",
            )
            add_column_if_missing(
                conn,
                "dispatch_envelopes",
                "route",
                "route TEXT",
            )
            add_column_if_missing(
                conn,
                "dispatch_envelopes",
                "session_id",
                "session_id TEXT",
            )
            for statement in _SESSION_INDEX_SCHEMA:
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
