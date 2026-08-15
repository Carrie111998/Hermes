"""SQLite connection and transaction ownership for the Kanban store."""

from __future__ import annotations

import contextlib
import random
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path


class TransactionBusy(RuntimeError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = str(Path(path).expanduser().resolve())
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def is_busy_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in text or "database is busy" in text
    )


@contextlib.contextmanager
def write_txn(
    conn: sqlite3.Connection,
    *,
    attempts: int = 8,
    base_delay: float = 0.01,
) -> Iterator[sqlite3.Connection]:
    """Own one ``BEGIN IMMEDIATE`` transaction with bounded busy retry."""

    if conn.in_transaction:
        # The caller already owns the transaction; do not create an accidental
        # second framework or commit a parent transaction.
        yield conn
        return

    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except BaseException as exc:  # sqlite exposes busy through OperationalError
            if not is_busy_error(exc):
                raise
            last = exc
            if attempt + 1 >= attempts:
                raise TransactionBusy("could not acquire Kanban write transaction") from exc
            delay = base_delay * (2**attempt) + random.random() * base_delay
            time.sleep(min(delay, 0.5))
    else:  # pragma: no cover - loop always breaks or raises
        raise TransactionBusy("could not acquire Kanban write transaction") from last

    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
