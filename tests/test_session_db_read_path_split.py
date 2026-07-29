"""Tests for the SessionDB read-path split (per-thread read-only connections).

The gateway shares ONE SessionDB across every agent, so recall/browse reads
used to queue behind writer flushes on self._lock — a measured production
convoy (a 0.2s FTS query stretched to 112s while 6-8 concurrent turns
flushed tool results). These tests pin the new contract: reads run on a
per-thread read-only connection under WAL, never touch self._lock, and fall
back to the legacy locked path when WAL or the read connection is missing.
"""

import threading

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    d.append_message("s1", role="user", content="hello graphiti world")
    d.append_message("s1", role="assistant", content="the neo4j daemon is healthy")
    yield d
    d.close()


@pytest.mark.requires_wal
def test_read_conn_is_per_thread(db):
    conns = {}

    def grab(key):
        conns[key] = db._get_read_conn()

    t1 = threading.Thread(target=grab, args=(1,))
    t2 = threading.Thread(target=grab, args=(2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert conns[1] is not None and conns[2] is not None
    assert conns[1] is not conns[2]


def test_read_conn_reused_within_thread(db):
    assert db._get_read_conn() is db._get_read_conn()


@pytest.mark.requires_wal
def test_reads_do_not_take_writer_lock(db):
    """Reads must complete while another thread holds self._lock."""
    acquired = db._lock.acquire()
    assert acquired
    try:
        done = {}

        def reader():
            done["session"] = db.get_session("s1")
            done["search"] = db.search_messages("graphiti", limit=10)
            done["messages"] = db.get_messages("s1")

        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive(), "read path blocked on writer lock"
        assert done["session"]["id"] == "s1"
        assert any("graphiti" in (m.get("snippet") or "") for m in done["search"])
        assert len(done["messages"]) == 2
    finally:
        db._lock.release()




def test_read_your_writes(db):
    """A fresh committed write must be visible to the read connection."""
    db.append_message("s1", role="user", content="zanzibar checkpoint")
    rows = db.search_messages("zanzibar", limit=5)
    assert rows, "committed write invisible to read connection"




def test_non_wal_uses_locked_path(db):
    db._wal_active = False
    assert db._get_read_conn() is None
    # And queries still work via the legacy path.
    assert db.get_session("s1")["id"] == "s1"


@pytest.mark.requires_wal
def test_read_conn_open_failure_marks_thread(db, monkeypatch, tmp_path):
    """A failed read-conn open must not retry per query; fallback still works."""
    import sqlite3 as _sqlite3

    calls = {"n": 0}
    real_connect = _sqlite3.connect

    def failing_connect(*a, **k):
        if a and isinstance(a[0], str) and a[0].startswith("file:") and "mode=ro" in a[0]:
            calls["n"] += 1
            raise _sqlite3.OperationalError("simulated open failure")
        return real_connect(*a, **k)

    fresh = SessionDB(db_path=tmp_path / "state2.db")
    try:
        fresh.create_session(session_id="x", source="cli", model="m")
        monkeypatch.setattr("hermes_state.sqlite3.connect", failing_connect)
        assert fresh.get_session("x")["id"] == "x"
        assert fresh.get_session("x")["id"] == "x"
        assert calls["n"] == 1, "open failure should be remembered per thread"
    finally:
        fresh.close()


@pytest.mark.requires_wal
def test_anchored_view_and_around_use_read_path(db):
    msgs = db.get_messages("s1")
    anchor = msgs[0]["id"]
    acquired = db._lock.acquire()
    try:
        done = {}

        def reader():
            done["around"] = db.get_messages_around("s1", anchor, window=2)
            done["view"] = db.get_anchored_view("s1", anchor, window=2, bookend=1)

        t = threading.Thread(target=reader)
        t.start(); t.join(timeout=5.0)
        assert not t.is_alive(), "anchored reads blocked on writer lock"
        assert done["around"]["window"]
        assert done["view"]["window"]
    finally:
        db._lock.release()


def _count_state_db_fds(db_path) -> int:
    """Count open fds whose path is this DB or its WAL/SHM sidecars."""
    import psutil

    root = str(db_path)
    return sum(
        1
        for f in psutil.Process().open_files()
        if f.path == root or f.path.startswith(root + "-")
    )


@pytest.mark.requires_wal
def test_read_conn_closeable_from_owner_thread(tmp_path):
    """Per-thread read conns must close from SessionDB.close()'s thread.

    Regression: _get_read_conn omitted check_same_thread=False, so
    SessionDB.close() on the owner thread raised ProgrammingError for every
    worker-created read conn, swallowed it, and leaked .db/.wal/.shm fds.
    Under gateway/dashboard thread pools this accumulated into EMFILE.
    """
    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        d.append_message("s1", role="user", content="fd leak probe")

        barriers = []
        errors = []

        def worker(i):
            try:
                assert d.get_session("s1")["id"] == "s1"
                # Keep the thread alive long enough for close() to run while
                # the connection objects still exist (registered in _read_conns).
                barriers[i].wait(timeout=5.0)
            except Exception as exc:  # pragma: no cover - surface in assert
                errors.append(exc)

        n_threads = 6
        barriers = [threading.Barrier(2) for _ in range(n_threads)]
        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        # Wait until every worker has opened its read conn.
        for i in range(n_threads):
            # Spin until this thread's conn is registered (or worker failed).
            for _ in range(200):
                if len(d._read_conns) >= i + 1 or errors:
                    break
                threading.Event().wait(0.01)
        assert not errors
        assert len(d._read_conns) >= n_threads

        # Cross-thread close of a worker-created read conn must succeed
        # (would raise ProgrammingError without check_same_thread=False).
        sample = next(iter(d._read_conns))
        sample.close()

        fds_before_close = _count_state_db_fds(d.db_path)
        assert fds_before_close > 0

        d.close()
        # Release barriers so workers exit cleanly after close.
        for b in barriers:
            try:
                b.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass
        for t in threads:
            t.join(timeout=5.0)

        fds_after = _count_state_db_fds(d.db_path)
        assert fds_after == 0, (
            f"SessionDB.close() left {fds_after} state.db fds open "
            f"(had {fds_before_close} before close); threaded read conns leaked"
        )
        assert len(d._read_conns) == 0
    finally:
        # Idempotent if already closed above.
        try:
            d.close()
        except Exception:
            pass


@pytest.mark.requires_wal
def test_close_after_threadpool_reads_reclaims_fds(tmp_path):
    """Thread-pool style reads (gateway/dashboard) must not leave FDs behind."""
    from concurrent.futures import ThreadPoolExecutor

    d = SessionDB(db_path=tmp_path / "pool.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        for i in range(5):
            d.append_message("s1", role="user", content=f"msg {i}")

        def read_once(_):
            return d.get_session("s1")["id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read_once, range(32)))
        assert results == ["s1"] * 32
        assert len(d._read_conns) >= 1

        d.close()
        assert _count_state_db_fds(d.db_path) == 0
        assert d._conn is None
        assert len(d._read_conns) == 0
    finally:
        try:
            d.close()
        except Exception:
            pass
