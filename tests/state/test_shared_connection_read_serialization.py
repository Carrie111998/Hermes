"""Regression coverage for reads sharing SessionDB's writer connection.

When WAL is unavailable, ``_read_ctx`` falls back to the writable connection.
Every operation on that connection must hold ``SessionDB._lock``: CPython's
sqlite3 statement cache releases the GIL while preparing SQL, so concurrent
use of one ``check_same_thread=False`` connection can lose the original SQLite
exception and surface ``SystemError: <Connection> returned NULL without
setting an exception`` instead.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    session_db = SessionDB(tmp_path / "state.db")
    session_db.create_session("shared-writer", "test")
    yield session_db
    session_db.close()


@pytest.mark.parametrize(
    "read_operation",
    [
        pytest.param(
            lambda db: db.get_compression_lock_holder("shared-writer"),
            id="compression-lock-holder",
        ),
        pytest.param(
            lambda db: db.clear_session_activity_labels("shared-writer"),
            id="activity-label-fast-path",
        ),
        pytest.param(
            lambda db: db.get_handoff_state("shared-writer"),
            id="handoff-state",
        ),
        pytest.param(
            lambda db: db.list_pending_handoffs(),
            id="pending-handoffs",
        ),
    ],
)
def test_non_wal_reads_wait_for_shared_writer_lock(
    db: SessionDB,
    read_operation: Callable[[SessionDB], object],
) -> None:
    """Fallback reads cannot enter the shared writer while it is in use."""
    db._wal_active = False
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def run_read() -> None:
        started.set()
        try:
            read_operation(db)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_read, daemon=True)
    with db._lock:
        worker.start()
        assert started.wait(1.0)
        assert not finished.wait(0.1), (
            "read touched the shared check_same_thread=False writer "
            "without SessionDB._lock"
        )

    assert finished.wait(2.0)
    worker.join(timeout=1.0)
    assert failures == []
