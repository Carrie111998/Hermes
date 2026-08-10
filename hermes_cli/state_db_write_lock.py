"""Cross-process write ownership for Hermes' canonical ``state.db``.

SQLite WAL supports concurrent readers but only one writer.  Hermes has a
long-lived SessionDB plus a few durable ledgers which historically opened
their own write connections.  SQLite serialises the transactions eventually,
but it cannot make independently managed WAL/checkpoint lifecycles safe.

This lock is deliberately only for write transactions and schema changes.
Read-only SessionDB connections remain lock-free and retain WAL concurrency.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def state_db_write_lock(db_path: Path) -> Iterator[None]:
    """Serialize a state.db write transaction across Hermes processes."""
    lock_path = db_path.with_name(f"{db_path.name}.write.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)