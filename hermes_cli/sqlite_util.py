"""Shared SQLite primitives for the small per-profile / board stores.

The projects and kanban stores open WAL SQLite files with the same two
primitives — an idempotent column-add migration and an IMMEDIATE write
transaction. One definition here keeps the two stores from drifting.
"""

from __future__ import annotations

import contextlib
import random
import sqlite3
import time
from pathlib import Path


MIN_BUSY_TIMEOUT_MS = 5_000
BUSY_RETRY_ATTEMPTS = 3
_BUSY_RETRY_DELAYS_S = (0.1, 0.2, 0.4)


def open_connection(
    path: Path,
    *,
    busy_timeout_ms: int = MIN_BUSY_TIMEOUT_MS,
    enable_wal: bool = True,
    synchronous: str = "FULL",
    db_label: str = "sqlite.db",
    row_factory: bool = True,
) -> sqlite3.Connection:
    """Open a consistently hardened SQLite connection.

    Every caller gets foreign-key enforcement and an observable busy timeout.
    Shared runtime stores also opt into the repository's WAL fallback policy.
    ``FULL`` is intentionally the default on macOS: Hermes's WAL helper
    already enforces it to avoid the WAL-reset/torn-page crash window.
    """
    normalized_timeout = max(MIN_BUSY_TIMEOUT_MS, int(busy_timeout_ms))
    normalized_synchronous = str(synchronous).strip().upper()
    if normalized_synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"unsupported SQLite synchronous mode: {synchronous!r}")

    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(resolved),
        isolation_level=None,
        timeout=normalized_timeout / 1000.0,
    )
    try:
        if row_factory:
            conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={normalized_timeout}")
        conn.execute("PRAGMA foreign_keys=ON")
        if enable_wal:
            from hermes_state import apply_wal_with_fallback

            apply_wal_with_fallback(conn, db_label=db_label)
            conn.execute(f"PRAGMA synchronous={normalized_synchronous}")
        return conn
    except Exception:
        conn.close()
        raise


def is_busy_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is a transient SQLite lock-contention error."""
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in str(exc).lower()
        or "database is busy" in str(exc).lower()
        or getattr(exc, "sqlite_errorcode", None)
        in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


def execute_boundary_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    *,
    retries: int = BUSY_RETRY_ATTEMPTS,
) -> None:
    """Execute a transaction boundary with bounded BUSY retry and jitter."""
    normalized_retries = max(0, int(retries))
    for attempt in range(normalized_retries + 1):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc) or attempt == normalized_retries:
                raise
            base_delay = _BUSY_RETRY_DELAYS_S[
                min(attempt, len(_BUSY_RETRY_DELAYS_S) - 1)
            ]
            time.sleep(base_delay + random.uniform(0.0, base_delay * 0.25))


@contextlib.contextmanager
def retrying_write_txn(
    conn: sqlite3.Connection,
    *,
    retries: int = BUSY_RETRY_ATTEMPTS,
):
    """An IMMEDIATE write transaction with BUSY-safe boundaries.

    Only ``BEGIN IMMEDIATE`` and ``COMMIT`` are retried. The transaction body
    is never replayed, so a caller cannot double-insert a paid-call ledger row
    after an ambiguous commit.
    """
    execute_boundary_with_retry(conn, "BEGIN IMMEDIATE", retries=retries)
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        try:
            execute_boundary_with_retry(conn, "COMMIT", retries=retries)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races.

    Returns ``True`` when this call added the column. Swallows the
    ``duplicate column name`` error a concurrent migrator may have run first
    (issue #21708). ``column`` is the human-readable name for the call site;
    ``ddl`` carries the actual definition.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """An IMMEDIATE write transaction: at most one concurrent writer wins.

    The explicit ROLLBACK is guarded so a SQLite auto-rollback (no active
    transaction left under EIO / lock contention / corruption) cannot shadow
    the original exception with a spurious rollback error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")
