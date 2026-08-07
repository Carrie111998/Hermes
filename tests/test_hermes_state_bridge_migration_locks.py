"""Regression tests: ``SessionDB()`` open must not fail with "database is locked".

Root cause of the 2026-08-07 "all desktop sessions disappeared" incident:
every ``SessionDB()`` open runs ``_init_schema`` -> the four bridge data
migrations, each of which issued ``BEGIN IMMEDIATE`` (a WAL *write* lock)
*before* checking whether the migration was already applied.  On a
fully-migrated database that write lock is pure waste; under transient write
contention from the session-bridge writer it raised
``sqlite3.OperationalError: database is locked`` -- 500ing the desktop's
read-only ``get_session_messages`` / session-list endpoints, which rendered an
empty session list even though every row was intact on disk.

Two guarantees:
  A. An already-applied bridge migration is a lock-free no-op (read pre-check
     before ``BEGIN IMMEDIATE``).
  B. A fresh open survives a write lock held longer than the single-shot
     SQLite busy timeout (bounded retry with jitter on the schema-ensure step).
"""

import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB

_MIGRATION_METHODS = [
    "_apply_bridge_migrations",
    "_apply_claude_characterization_abort_trigger_migration",
    "_apply_claude_characterization_events_v28_migration",
    "_apply_claude_auth_recovery_call_started_migration",
]


def _hold_write_lock(db_path):
    """Open a raw connection holding ``BEGIN IMMEDIATE`` (the WAL write lock)."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA busy_timeout=0")
    conn.execute("BEGIN IMMEDIATE")
    return conn


@pytest.mark.parametrize("migration_method", _MIGRATION_METHODS)
def test_applied_bridge_migration_is_lockfree_noop(tmp_path, migration_method):
    """An already-applied migration must not acquire the write lock.

    Reproduces the exact production traceback: with another connection holding
    ``BEGIN IMMEDIATE``, re-running an already-applied migration must return
    without raising ``database is locked``.
    """
    db_path = tmp_path / "state.db"
    sdb = SessionDB(db_path=db_path)  # first open applies every migration
    blocker = _hold_write_lock(db_path)
    try:
        cursor = sdb._conn.cursor()
        # Already recorded in session_bridge_migrations -> must be a no-op that
        # never reaches BEGIN IMMEDIATE.
        getattr(sdb, migration_method)(cursor)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        sdb.close()


def test_open_survives_transient_write_lock(tmp_path):
    """A fresh ``SessionDB()`` open must survive a transient write-lock hold.

    The session bridge briefly holds the WAL write lock; the desktop backend
    opens ``SessionDB`` per request. Opening must retry past a lock held longer
    than the single-shot busy timeout instead of 500ing the read endpoint.
    """
    db_path = tmp_path / "state.db"
    SessionDB(db_path=db_path).close()  # migrate once

    hold_secs = 1.3  # > the 1.0s connect() timeout -> reproduces the incident
    started = threading.Event()

    def _hold():
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(hold_secs)
        conn.execute("ROLLBACK")
        conn.close()

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert started.wait(timeout=5), "lock holder failed to start"
        # Must NOT raise sqlite3.OperationalError("database is locked").
        sdb = SessionDB(db_path=db_path)
        sdb.close()
    finally:
        holder.join()
