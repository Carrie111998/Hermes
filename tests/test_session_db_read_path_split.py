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


@pytest.mark.requires_wal
def test_session_resume_reads_do_not_take_writer_lock(db):
    """session.resume's three read paths must not convoy behind writer flushes.

    get_messages_as_conversation / get_resume_conversations /
    get_ancestor_display_prefix are the hottest reads in the file — every
    resume across the gateway, CLI, and ACP adapter goes through one of
    them — so they must use the same per-thread read-only connection as
    get_messages, not the legacy self._lock path.
    """
    db.create_session(session_id="parent1", source="cli", model="m")
    db.append_message("parent1", role="user", content="parent turn")
    db.append_message("parent1", role="assistant", content="parent reply")
    db.create_session(session_id="child1", source="cli", model="m", parent_session_id="parent1")
    db.append_message("child1", role="user", content="child turn")
    db.append_message("child1", role="assistant", content="child reply")

    acquired = db._lock.acquire()
    try:
        done = {}

        def reader():
            done["conversation"] = db.get_messages_as_conversation("s1")
            done["resume"] = db.get_resume_conversations("child1")
            done["ancestor_prefix"] = db.get_ancestor_display_prefix("child1")

        t = threading.Thread(target=reader)
        t.start(); t.join(timeout=5.0)
        assert not t.is_alive(), "session resume reads blocked on writer lock"
        assert len(done["conversation"]) == 2
        model_history, display_history = done["resume"]
        assert len(model_history) == 2
        assert len(display_history) == 4
        assert len(done["ancestor_prefix"]) == 2
    finally:
        db._lock.release()


# ── #75269: finished-thread read connections must be reaped at runtime ──


def _force_read_path(d):
    """Activate the per-thread read-only split on runtimes where WAL is off."""
    d._wal_active = True


def test_finished_read_threads_do_not_accumulate_conns(tmp_path):
    """A long-lived SessionDB must not retain a read conn per historical worker."""
    import gc

    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)

        def read_once():
            assert d._get_read_conn() is not None

        for _ in range(40):
            worker = threading.Thread(target=read_once)
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive()

        gc.collect()
        retained = len(d._read_conns)
        assert retained == 1, (
            f"finished worker threads retained {retained} read connections; "
            "only the final worker's connection may remain (#75269)"
        )
    finally:
        d.close()


def test_reaped_connection_is_actually_closed(tmp_path):
    """A connection whose owning thread exited must be closed on reap."""
    import sqlite3 as _sqlite3

    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        holder = {}

        def worker_open():
            holder["conn"] = d._get_read_conn()

        worker = threading.Thread(target=worker_open)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        victim = holder["conn"]
        assert victim is not None

        def worker_reap():
            assert d._get_read_conn() is not None

        reaper = threading.Thread(target=worker_reap)
        reaper.start()
        reaper.join(timeout=10)
        assert not reaper.is_alive()

        assert victim not in d._read_conns, "dead-thread connection was not reaped"
        with pytest.raises(_sqlite3.ProgrammingError):
            victim.execute("SELECT 1")
    finally:
        d.close()


def test_live_thread_connection_not_reaped(tmp_path):
    """A connection whose owner thread is still alive must survive a reap."""
    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        ready = threading.Event()
        release = threading.Event()
        live_conn = {}

        def hold():
            live_conn["conn"] = d._get_read_conn()
            ready.set()
            release.wait(timeout=10)

        keeper = threading.Thread(target=hold)
        keeper.start()
        assert ready.wait(timeout=10)
        conn = live_conn["conn"]
        assert conn is not None

        for _ in range(5):
            worker = threading.Thread(target=lambda: d._get_read_conn())
            worker.start()
            worker.join(timeout=10)

        assert keeper.is_alive(), "live reader thread died unexpectedly"
        assert conn in d._read_conns, "live-thread connection was wrongly reaped"

        release.set()
        keeper.join(timeout=10)
    finally:
        d.close()


def test_reaping_is_thread_safe_under_concurrency(tmp_path):
    """Concurrent readers and reaping must not raise."""
    import time

    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        d.append_message("s1", role="user", content="concurrency smoke")
        errors = []

        def reader():
            try:
                for _ in range(20):
                    d.get_session("s1")
                    d._get_read_conn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for index, thread in enumerate(threads):
            thread.start()
            if index % 2:
                time.sleep(0.001)
        for thread in threads:
            thread.join(timeout=20)

        assert not errors, f"concurrent read/reap raised: {errors!r}"
        assert not any(thread.is_alive() for thread in threads)
    finally:
        d.close()


def test_close_waits_for_active_read_context(tmp_path, monkeypatch):
    """Teardown must not close a read connection while its owner is using it."""
    d = SessionDB(db_path=tmp_path / "state.db")
    release = threading.Event()
    reader_done = threading.Event()
    close_done = threading.Event()
    victim_closed = threading.Event()
    entered = threading.Event()
    errors = []
    threads = []
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        holder = {}

        def reader():
            try:
                with d._read_ctx() as conn:
                    assert conn is not None
                    holder["conn"] = conn
                    entered.set()
                    assert release.wait(timeout=10)
                    assert conn.execute("SELECT 1").fetchone()[0] == 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                reader_done.set()

        reader_thread = threading.Thread(target=reader)
        threads.append(reader_thread)
        reader_thread.start()
        assert entered.wait(timeout=10)

        victim = holder["conn"]
        original_close = type(victim).close

        def observed_close(conn):
            try:
                return original_close(conn)
            finally:
                if conn is victim:
                    victim_closed.set()

        monkeypatch.setattr(type(victim), "close", observed_close)

        def closer():
            try:
                d.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                close_done.set()

        close_thread = threading.Thread(target=closer)
        threads.append(close_thread)
        close_thread.start()

        assert not victim_closed.wait(timeout=1), (
            "close() closed a read connection while its context was still active"
        )
        assert not close_done.is_set(), (
            "close() returned before the active reader drained"
        )

        release.set()
        assert reader_done.wait(timeout=10)
        assert close_done.wait(timeout=10)
        assert victim_closed.is_set()
        assert not errors, f"reader/closer race raised: {errors!r}"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        d.close()


def test_cached_read_connection_is_not_returned_after_close(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    _force_read_path(d)
    assert d._get_read_conn() is not None

    d.close()

    assert d._get_read_conn() is None
    with pytest.raises(RuntimeError, match="SessionDB is closed"):
        with d._read_ctx():
            pass
    d.close()


def test_close_from_active_read_context_fails_without_deadlock(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)

        with d._read_ctx():
            with pytest.raises(RuntimeError, match="active read context"):
                d.close()
    finally:
        d.close()


def test_failed_reap_remains_tracked_for_retry(tmp_path, monkeypatch):
    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        holder = {}

        def worker_open():
            holder["conn"] = d._get_read_conn()

        worker = threading.Thread(target=worker_open)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        victim = holder["conn"]
        assert victim is not None
        original_close = type(victim).close

        def failing_close(conn):
            if conn is victim:
                raise RuntimeError("synthetic close failure")
            return original_close(conn)

        monkeypatch.setattr(type(victim), "close", failing_close)
        with d._read_conns_lock:
            d._reap_dead_read_conns()
        assert victim in d._read_conns

        monkeypatch.setattr(type(victim), "close", original_close)
        with d._read_conns_lock:
            d._reap_dead_read_conns()
        assert victim not in d._read_conns
    finally:
        d.close()


def test_read_connection_finishing_open_after_close_is_closed(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    import hermes_state as hs

    d = SessionDB(db_path=tmp_path / "state.db")
    opened = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    close_done = threading.Event()
    holder = {}
    errors = []
    worker = None
    closer = None
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        real_connect = hs._connect_tracked_db

        def delayed_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            holder["conn"] = conn
            opened.set()
            assert release.wait(timeout=10)
            return conn

        monkeypatch.setattr(hs, "_connect_tracked_db", delayed_connect)

        def open_read_connection():
            try:
                holder["result"] = d._get_read_conn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=open_read_connection)
        worker.start()
        assert opened.wait(timeout=10)

        def close_database():
            close_started.set()
            try:
                d.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                close_done.set()

        closer = threading.Thread(target=close_database)
        closer.start()
        assert close_started.wait(timeout=10)
        assert not close_done.wait(timeout=1), (
            "close() returned while a read connection was still opening"
        )

        release.set()
        worker.join(timeout=10)
        closer.join(timeout=10)

        assert not worker.is_alive()
        assert not closer.is_alive()
        assert close_done.is_set()
        assert not errors
        assert holder["result"] is None
        assert holder["conn"] not in d._read_conns
        with pytest.raises(_sqlite3.ProgrammingError):
            holder["conn"].execute("SELECT 1")
    finally:
        release.set()
        if worker is not None:
            worker.join(timeout=10)
        if closer is not None:
            closer.join(timeout=10)
        d.close()


def test_failed_close_remains_tracked_for_retry(tmp_path, monkeypatch):
    d = SessionDB(db_path=tmp_path / "state.db")
    try:
        d.create_session(session_id="s1", source="cli", model="m")
        _force_read_path(d)
        holder = {}

        def worker_open():
            holder["conn"] = d._get_read_conn()

        worker = threading.Thread(target=worker_open)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        victim = holder["conn"]
        assert victim is not None
        original_close = type(victim).close

        def failing_close(conn):
            if conn is victim:
                raise RuntimeError("synthetic close failure")
            return original_close(conn)

        monkeypatch.setattr(type(victim), "close", failing_close)
        d.close()
        assert victim in d._read_conns

        monkeypatch.setattr(type(victim), "close", original_close)
        d.close()
        assert victim not in d._read_conns
    finally:
        d.close()
