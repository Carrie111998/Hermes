"""Regression: the async-delegation ledger must close every SQLite connection.

Sibling of the cron execution-ledger leak (#69567 / PR #69594). The durable
delegation ledger used ``with _connect() as conn:`` where the connection
context manager commits/rolls back but never closes, leaking the db/-wal/-shm
file descriptors on every dispatch, completion, and delivery-claim. These tests
fail if the deterministic ``close()`` is ever removed again.
"""

import queue
import sqlite3

import pytest

from tools import async_delegation as ad


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
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    return ad


def _track_connections(monkeypatch):
    opened, closed = [], []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(id(conn))
        return _TrackingConnection(conn, closed)

    monkeypatch.setattr(ad.sqlite3, "connect", tracking_connect)
    return opened, closed


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Public durable-ledger reads/writes must close every connection opened."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = _track_connections(monkeypatch)

    ad.get_durable_delegation("nope")
    ad.recover_abandoned_delegations()
    ad.restore_undelivered_completions(queue.Queue())
    ad.mark_completion_delivered("nope")
    ad.claim_completion_delivery("nope", "claim-1")

    assert opened, "expected at least one connection to be opened"
    assert len(opened) == len(closed)
    assert set(opened) == set(closed)


def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
    """A PRAGMA/DDL failure after connect() must still close the connection."""
    _point_ledger(monkeypatch, tmp_path)
    opened, closed = [], []
    real_connect = sqlite3.connect

    class _FailingSchemaConnection(_TrackingConnection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE TABLE" in sql:
                raise sqlite3.OperationalError("simulated schema init failure")
            return self._real.execute(sql, *args, **kwargs)

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(id(conn))
        return _FailingSchemaConnection(conn, closed)

    monkeypatch.setattr(ad.sqlite3, "connect", tracking_connect)

    with pytest.raises(sqlite3.OperationalError):
        with ad._transaction():
            pass

    assert len(opened) == 1
    assert len(closed) == 1


def _seed_delete_mode(path):
    """Create state.db in journal_mode=DELETE as a guest would inherit it."""
    seed = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        seed.execute("PRAGMA journal_mode=DELETE")
    finally:
        seed.close()


def test_guest_delete_mode_preserved_and_contention_survived(monkeypatch, tmp_path):
    """async_delegation is a GUEST of state.db: it must not change journal
    mode (DELETE stays DELETE) and must ride out transient write contention
    via the shared BEGIN IMMEDIATE primitive.

    This is the mandatory vertical regression: seed journal_mode=DELETE
    while the ledger initializes, run a real guest dispatch, and confirm both
    the operation succeeds AND journal mode remains `delete`.
    """
    import threading

    _point_ledger(monkeypatch, tmp_path)
    path = tmp_path / "state.db"

    # Let _connect() create the schema first so columns exist; then force
    # DELETE mode explicitly (the guest ownership contract).
    ad._connect().close()
    _seed_delete_mode(path)
    mode_before = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode_before == "delete", f"seed failed, mode={mode_before!r}"

    # Competitor holds the write lock briefly.
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

    # Guest dispatch must succeed by riding out the hold.
    ad._persist_dispatch({
        "delegation_id": "deleg_contended",
        "session_key": "test:sess",
        "origin_ui_session_id": "",
        "parent_session_id": None,
        "dispatched_at": 1.0,
        "origin_session_id": "",
        "goal": "contended",
        "context": "ctx",
    })
    row = ad.get_durable_delegation("deleg_contended")
    assert row is not None, row
    assert row["origin_session"] == "test:sess"

    competitor.join(timeout=2.0)

    # DELETE mode must be preserved — the guest changed nothing.
    mode_after = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode_after == "delete", (
        f"async_delegation guest changed journal mode to {mode_after!r}"
    )


def test_guest_uncontended_delete_mode_preserved(monkeypatch, tmp_path):
    """Even without contention, a guest dispatch must leave DELETE mode intact."""
    _point_ledger(monkeypatch, tmp_path)
    path = tmp_path / "state.db"

    ad._connect().close()
    _seed_delete_mode(path)

    ad._persist_dispatch({
        "delegation_id": "deleg_plain",
        "session_key": "test:plain",
        "origin_ui_session_id": "",
        "parent_session_id": None,
        "dispatched_at": 1.0,
        "origin_session_id": "",
        "goal": "plain",
        "context": "ctx",
    })
    assert ad.get_durable_delegation("deleg_plain") is not None

    mode = str(
        sqlite3.connect(str(path), timeout=10, isolation_level=None)
        .execute("PRAGMA journal_mode").fetchone()[0]
    ).lower()
    assert mode == "delete", f"guest changed journal mode to {mode!r}"
