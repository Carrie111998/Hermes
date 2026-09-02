"""Regression: the gateway delivery ledger must close every SQLite connection.

Sibling of the cron execution-ledger leak (#69567 / PR #69594). The ledger used
``with _connect() as conn:`` where ``sqlite3.Connection.__exit__`` commits or
rolls back but never closes, leaking the db/-wal/-shm file descriptors on every
call until a long-running gateway exhausts ``RLIMIT_NOFILE``. ``record_obligation``
runs on every outbound final response, so this is the highest-frequency leaker of
the set. These tests fail if the deterministic ``close()`` is ever removed again.
"""

import sqlite3

import pytest

from gateway import delivery_ledger as dl


class _TrackingConnection:
    """Delegates to a real sqlite3.Connection while recording close() calls.

    sqlite3.Connection is a static C type: it has no per-instance __dict__ and
    its methods can't be monkeypatched, so open/close tracking is done via a
    delegating wrapper returned in place of the real connection.
    """

    def __init__(self, real, closed_ids):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_closed_ids", closed_ids)

    def close(self):
        self._closed_ids.append(id(self._real))
        self._real.close()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _point_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    return dl


def _track_connections(monkeypatch):
    opened, closed = [], []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(id(conn))
        return _TrackingConnection(conn, closed)

    monkeypatch.setattr(dl.sqlite3, "connect", tracking_connect)
    return opened, closed


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Every public ledger operation must close the connection it opened."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = _track_connections(monkeypatch)

    oid = dl.compute_obligation_id("sess", "msg", "content")
    dl.record_obligation(
        obligation_id=oid, session_key="sess", platform="telegram",
        chat_id="123", thread_id=None, content="hello",
    )
    dl.mark_attempting(oid)
    dl.mark_delivered(oid)
    dl.sweep_recoverable()
    dl.debug_rows()

    assert opened, "expected at least one connection to be opened"
    assert len(opened) == len(closed)
    assert set(opened) == set(closed)


def _seed_delete_mode(path):
    """Create state.db in journal_mode=DELETE as a guest would inherit it."""
    seed = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        seed.execute("PRAGMA journal_mode=DELETE")
    finally:
        seed.close()


def test_guest_delete_mode_preserved_and_contention_survived(monkeypatch, tmp_path):
    """delivery_ledger is a GUEST of state.db: it must not establish journal
    mode (DELETE stays DELETE) and must ride out transient write contention
    via the shared BEGIN IMMEDIATE primitive.

    Mandatory vertical regression: seed journal_mode=DELETE, run a real
    ledger write, confirm the write succeeds and journal mode stays delete.
    """
    import threading

    _point_ledger(monkeypatch, tmp_path)
    path = tmp_path / "state.db"

    # Let _connect() create the schema, then force DELETE mode explicitly.
    dl._connect().close()
    _seed_delete_mode(path)
    mode_before = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode_before == "delete", f"seed failed, mode={mode_before!r}"

    competitor_holder = []
    release = threading.Event()

    def _hold_lock():
        c = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        competitor_holder.append(c)
        try:
            c.execute("BEGIN IMMEDIATE")
        except Exception:
            return
        release.wait()
        try:
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
        finally:
            c.close()

    competitor = threading.Thread(target=_hold_lock, daemon=True)
    competitor.start()

    import time as _time
    deadline = _time.monotonic() + 2.0
    while not competitor_holder:
        if _time.monotonic() > deadline:
            pytest.fail("competitor never acquired the lock")
        _time.sleep(0.01)
    _time.sleep(0.05)

    def _release_after():
        _time.sleep(0.25)
        release.set()

    threading.Thread(target=_release_after, daemon=True).start()

    # Guest ledger write must succeed by riding out the hold.
    dl.record_obligation(
        obligation_id="obl_contended",
        session_key="sess",
        platform="telegram",
        chat_id="123",
        thread_id=None,
        content="contended write",
    )

    import json as _json
    parsed = _json.loads(dl.debug_rows())
    found = [r for r in parsed if r["id"] == "obl_contended"]
    assert found, parsed

    competitor.join(timeout=2.0)

    # DELETE mode must be preserved — the guest changed nothing.
    mode_after = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode_after == "delete", (
        f"delivery_ledger guest changed journal mode to {mode_after!r}"
    )


def test_guest_uncontended_delete_mode_preserved(monkeypatch, tmp_path):
    """Even without contention, a guest ledger write leaves DELETE mode intact."""
    _point_ledger(monkeypatch, tmp_path)
    path = tmp_path / "state.db"

    dl._connect().close()
    _seed_delete_mode(path)

    dl.record_obligation(
        obligation_id="obl_plain",
        session_key="sess",
        platform="telegram",
        chat_id="456",
        thread_id=None,
        content="plain write",
    )

    import json as _json
    parsed = _json.loads(dl.debug_rows())
    assert any(r["id"] == "obl_plain" for r in parsed), parsed

    mode = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode == "delete", f"guest changed journal mode to {mode!r}"


