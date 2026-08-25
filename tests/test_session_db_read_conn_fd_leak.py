"""Regression coverage for SessionDB WAL reader connection lifetimes."""

import threading

import hermes_state
from hermes_cli import sqlite_safe_read
from hermes_state import SessionDB


def _live_connection_count(path):
    key = sqlite_safe_read._key(path)
    with sqlite_safe_read._live_lock:
        return sqlite_safe_read._live_connections.get(key, 0)


def test_short_lived_readers_do_not_accumulate_tracked_connections(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        hermes_state,
        "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.create_session(session_id="probe", source="cli", model="m")
        baseline = _live_connection_count(db_path)
        counts = []

        for iteration in range(25):
            done = threading.Event()

            def read_once():
                db.get_session("probe")
                done.set()

            thread = threading.Thread(
                target=read_once,
                name=f"state-db-reader-{iteration}",
            )
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert done.is_set()
            counts.append(_live_connection_count(db_path))

        # A bounded reader pool may retain a small fixed number of open
        # connections, but the count must not grow once per reader thread.
        assert max(counts) <= baseline + 8
    finally:
        db.close()

    assert _live_connection_count(db_path) == 0
