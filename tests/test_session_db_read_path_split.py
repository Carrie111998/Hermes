"""Tests for the SessionDB read-path split (per-thread read-only connections).

The gateway shares ONE SessionDB across every agent, so recall/browse reads
used to queue behind writer flushes on self._lock — a measured production
convoy (a 0.2s FTS query stretched to 112s while 6-8 concurrent turns
flushed tool results). These tests pin the new contract: reads run on a
per-thread read-only connection under WAL, never touch self._lock, and fall
back to the legacy locked path when WAL or the read connection is missing.
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
import threading
import textwrap

import pytest

from hermes_cli.sqlite_safe_read import _live_connections
from hermes_state import SessionDB, _ThreadReadConnection


class _BlockingCloseConnection:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5.0)


class _FailOnceCloseConnection:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("injected close failure")


class _FailOnceTrackedCloseConnection:
    """Fail before delegating once, then close the real tracked connection."""

    def __init__(self, conn):
        self._conn = conn
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("injected orphan close failure")
        self._conn.close()


class _FailNTimesTrackedCloseConnection(_FailOnceTrackedCloseConnection):
    def __init__(self, conn, failures):
        super().__init__(conn)
        self._failures = failures

    def close(self):
        self.close_calls += 1
        if self.close_calls <= self._failures:
            raise RuntimeError("injected orphan close failure")
        self._conn.close()


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
def test_read_conn_setup_failure_closes_unregistered_tracked_connection(tmp_path, monkeypatch):
    """A post-connect setup failure must not leave an unowned tracked reader."""
    db_path = tmp_path / "state.db"
    tracked_path = str(db_path.resolve())
    baseline = _live_connections.get(tracked_path, 0)
    db = SessionDB(db_path=db_path)
    assert db._wal_active
    assert _live_connections.get(tracked_path, 0) == baseline + 1

    def fail_pragmas(conn, *, db_label):
        raise sqlite3.DatabaseError("injected reader pragma failure")

    monkeypatch.setattr("hermes_state.apply_database_pragmas", fail_pragmas)
    try:
        assert db._get_read_conn() is None
        assert db._read_local.failed
        assert not db._read_conns
        assert not db._read_conn_holders
        assert not db._failed_read_conn_holders
        assert _live_connections.get(tracked_path, 0) == baseline + 1
    finally:
        db.close()
    assert _live_connections.get(tracked_path, 0) == baseline


@pytest.mark.requires_wal
def test_close_waits_for_an_active_real_sqlite_reader_in_subprocess(tmp_path):
    """A close must never race SQLite's active VM; that can segfault THREADSAFE=2."""
    script = textwrap.dedent(
        """
        import json
        import sys
        import threading
        from pathlib import Path

        from hermes_cli.sqlite_safe_read import _live_connections
        from hermes_state import SessionDB

        db_path = Path(sys.argv[1]) / "state.db"
        tracked_path = str(db_path.resolve())
        baseline = _live_connections.get(tracked_path, 0)
        db = SessionDB(db_path=db_path)
        assert db._wal_active
        entered = threading.Event()
        release = threading.Event()
        reader_done = threading.Event()
        close_done = threading.Event()
        errors = []

        def block():
            entered.set()
            if not release.wait(5.0):
                raise RuntimeError("reader release timed out")
            return 1

        def reader():
            try:
                with db._read_ctx() as conn:
                    conn.create_function("block_for_close", 0, block)
                    assert conn.execute("SELECT block_for_close()").fetchone()[0] == 1
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                reader_done.set()

        def closer():
            try:
                db.close()
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                close_done.set()

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        assert entered.wait(5.0)
        close_thread = threading.Thread(target=closer)
        close_thread.start()
        assert not close_done.wait(0.3), "close returned while SQL was still active"
        assert reader_thread.is_alive(), "active query died while close was waiting"
        release.set()
        reader_thread.join(5.0)
        close_thread.join(5.0)
        assert not reader_thread.is_alive()
        assert not close_thread.is_alive()
        assert reader_done.is_set() and close_done.is_set()
        assert not errors
        assert not db._read_conns
        assert not db._read_conn_holders
        assert not db._failed_read_conn_holders
        assert _live_connections.get(tracked_path, 0) == baseline
        print(json.dumps({"ok": True}))
        """
    )
    env = os.environ.copy()
    repo_root = os.getcwd()
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.requires_wal
def test_close_rejects_new_read_leases_while_an_existing_reader_drains(db):
    """Terminal shutdown rejects later reads before it claims idle readers."""
    entered = threading.Event()
    release = threading.Event()
    reader_done = threading.Event()
    close_done = threading.Event()

    def reader():
        with db._read_ctx():
            entered.set()
            assert release.wait(timeout=5.0)
        reader_done.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert entered.wait(timeout=5.0)
    def closer():
        try:
            db.close()
        finally:
            close_done.set()

    close_thread = threading.Thread(target=closer)
    close_thread.start()
    try:
        with db._read_lease_cond:
            assert db._read_conns_closed
        assert not close_done.wait(0.3)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            db.get_session("s1")
    finally:
        release.set()
        reader_thread.join(timeout=5.0)
        close_thread.join(timeout=5.0)
    assert reader_done.is_set() and close_done.is_set()


@pytest.mark.parametrize("read_mode", ["wal", "fallback"])
def test_close_inside_read_context_fails_fast_without_poisoning_shutdown(tmp_path, read_mode):
    """A lease owner cannot synchronously close itself, including nested reads.

    The subprocess turns the pre-fix self-wait into a bounded, deterministic
    test failure instead of leaving the pytest worker hung. A failed close must
    not make the database terminal: after every lease exits, a normal close
    still drains every reader and tracked connection.
    """
    script = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        from hermes_cli.sqlite_safe_read import _live_connections
        from hermes_state import SessionDB

        db_path = Path(sys.argv[1]) / "state.db"
        mode = sys.argv[2]
        tracked_path = str(db_path.resolve())
        baseline = _live_connections.get(tracked_path, 0)
        db = SessionDB(db_path=db_path)
        if mode == "fallback":
            db._wal_active = False
        else:
            assert db._wal_active

        with db._read_ctx():
            if mode == "wal":
                with db._read_ctx():
                    try:
                        db.close()
                    except RuntimeError as exc:
                        assert "read lease" in str(exc)
                    else:
                        raise AssertionError("close inside nested read context did not fail")
                assert not db._read_conns_closed
                assert db._active_read_leases == 1
            try:
                db.close()
            except RuntimeError as exc:
                assert "read lease" in str(exc)
            else:
                raise AssertionError("close inside read context did not fail")

        assert db._active_read_leases == 0
        assert not db._read_conns_closed
        db.close()
        assert db._read_conns_closed
        assert not db._read_conns
        assert not db._read_conn_holders
        assert not db._failed_read_conn_holders
        assert _live_connections.get(tracked_path, 0) == baseline
        print(json.dumps({"ok": True, "mode": mode}))
        """
    )
    env = os.environ.copy()
    repo_root = os.getcwd()
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), read_mode],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"close inside {read_mode} read context self-deadlocked: {exc}")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True, "mode": read_mode}


@pytest.mark.requires_wal
def test_read_lease_releases_when_sql_raises(db):
    """A failing query cannot leave shutdown permanently waiting for its lease."""
    with pytest.raises(sqlite3.OperationalError):
        with db._read_ctx() as conn:
            conn.execute("SELECT no_such_sql_function()")
    assert db._active_read_leases == 0


@pytest.mark.requires_wal
def test_close_from_another_thread_drains_a_live_real_reader(db):
    """Shutdown can close an idle live worker's real SQLite reader without leaking it."""
    tracked_path = str(db.db_path.resolve())
    # The fixture's writer is the only connection before the worker starts;
    # compare against the external baseline after close drains that writer too.
    baseline_connections = _live_connections[tracked_path] - 1
    reader_opened = threading.Event()
    allow_worker_exit = threading.Event()
    worker_errors = []

    def reader():
        try:
            conn = db._get_read_conn()
            assert conn is not None
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            reader_opened.set()
            assert allow_worker_exit.wait(timeout=5.0)
        except Exception as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=reader)
    worker.start()
    assert reader_opened.wait(timeout=5.0)

    db.close()
    allow_worker_exit.set()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert not worker_errors
    assert not db._read_conns
    assert not db._read_conn_holders
    assert not db._failed_read_conn_holders
    assert _live_connections.get(tracked_path, 0) == baseline_connections


@pytest.mark.requires_wal
def test_close_and_worker_teardown_have_one_reader_close_owner(db):
    """Shutdown claims a holder before a blocked worker teardown can close it."""
    conn = _BlockingCloseConnection()
    holder = _ThreadReadConnection(db, conn)
    with db._read_conns_lock:
        db._read_conns.add(conn)
        db._read_conn_holders[conn] = holder

    shutdown = threading.Thread(target=db.close)
    shutdown.start()
    assert conn.entered.wait(timeout=5.0)

    teardown = threading.Thread(target=holder.close)
    teardown.start()
    teardown.join(timeout=5.0)
    assert not teardown.is_alive()
    assert conn.close_calls == 1

    conn.release.set()
    shutdown.join(timeout=5.0)
    assert not shutdown.is_alive()


@pytest.mark.requires_wal
def test_failed_reader_close_stays_tracked_and_is_retryable(db, caplog):
    """A failed holder close remains observable and available to retry."""
    conn = _FailOnceCloseConnection()
    holder = _ThreadReadConnection(db, conn)
    with db._read_conns_lock:
        db._read_conns.add(conn)
        db._read_conn_holders[conn] = holder

    assert not db._close_read_holder(holder)
    assert conn in db._read_conns
    assert db._read_conn_holders[conn] is holder
    assert "state.db read connection close failed" in caplog.text

    assert db._close_read_holder(holder)
    assert conn.close_calls == 2
    assert conn not in db._read_conns


@pytest.mark.requires_wal
def test_failed_reader_close_remains_retryable_after_worker_holder_is_gone(db):
    """A failing finalizer cannot strand a tracked connection without an owner."""
    conn = _FailOnceCloseConnection()
    holder = _ThreadReadConnection(db, conn)
    with db._read_conns_lock:
        db._read_conns.add(conn)
        db._read_conn_holders[conn] = holder

    assert not db._close_read_holder(holder)
    del holder
    gc.collect()
    assert db._failed_read_conn_holders[conn].conn is conn

    db.close()
    assert conn.close_calls == 2
    assert conn not in db._read_conns


def test_orphaned_holder_retains_a_failed_tracked_close_for_later_retry(tmp_path):
    """A dead SessionDB cannot strand a tracked reader after its first close fails."""
    import hermes_state

    db_path = tmp_path / "state.db"
    tracked_path = str(db_path.resolve())
    db = SessionDB(db_path=db_path)
    db.close()
    baseline = _live_connections.get(tracked_path, 0)
    tracked_conn = hermes_state._connect_tracked_db(
        f"file:{db_path}?mode=ro",
        tracking_path=db_path,
        uri=True,
        check_same_thread=False,
        isolation_level=None,
    )
    conn = _FailOnceTrackedCloseConnection(tracked_conn)
    holder = _ThreadReadConnection(db, conn)
    del db
    gc.collect()
    assert holder._db_ref() is None
    assert _live_connections.get(tracked_path, 0) == baseline + 1

    holder.close()

    assert holder.conn is conn
    assert holder in hermes_state._orphaned_read_conn_holders
    assert _live_connections.get(tracked_path, 0) == baseline + 1

    hermes_state._retry_orphaned_read_connection_closes()

    assert conn.close_calls == 2
    assert holder.conn is None
    assert holder not in hermes_state._orphaned_read_conn_holders
    assert _live_connections.get(tracked_path, 0) == baseline


def test_orphaned_close_retries_after_repeated_failures_at_lifecycle_boundaries(tmp_path):
    """Init and close retry a retained orphan until its tracked fd is released."""
    import hermes_state

    db_path = tmp_path / "state.db"
    tracked_path = str(db_path.resolve())
    original_db = SessionDB(db_path=db_path)
    original_db.close()
    baseline = _live_connections.get(tracked_path, 0)
    tracked_conn = hermes_state._connect_tracked_db(
        f"file:{db_path}?mode=ro",
        tracking_path=db_path,
        uri=True,
        check_same_thread=False,
        isolation_level=None,
    )
    conn = _FailNTimesTrackedCloseConnection(tracked_conn, failures=2)
    holder = _ThreadReadConnection(original_db, conn)
    del original_db
    gc.collect()

    holder.close()
    assert conn.close_calls == 1
    assert holder in hermes_state._orphaned_read_conn_holders

    retrying_db = SessionDB(db_path=db_path)
    assert conn.close_calls == 2
    assert holder in hermes_state._orphaned_read_conn_holders
    assert _live_connections.get(tracked_path, 0) == baseline + 2

    retrying_db.close()
    assert conn.close_calls == 3
    assert holder.conn is None
    assert holder not in hermes_state._orphaned_read_conn_holders
    assert _live_connections.get(tracked_path, 0) == baseline


@pytest.mark.requires_wal
def test_readers_after_close_fail_without_reusing_or_opening_connections(db, monkeypatch):
    """Close is terminal: cached and later-thread reads use no reader fd."""
    cached = db._get_read_conn()
    assert cached is not None
    tracked_path = str(db.db_path.resolve())
    real_connect = __import__("hermes_state")._connect_tracked_db
    opens = []

    def record_open(*args, **kwargs):
        opens.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("hermes_state._connect_tracked_db", record_open)
    db.close()

    assert db._get_read_conn() is None
    result = {}

    def later_reader():
        result["conn"] = db._get_read_conn()

    thread = threading.Thread(target=later_reader)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result["conn"] is None
    assert opens == []
    assert _live_connections.get(tracked_path, 0) == 0


@pytest.mark.requires_wal
def test_public_reads_after_close_raise_programming_error_for_cached_and_later_threads(db):
    """SessionDB's terminal lifecycle error is stable across reader threads."""
    assert db._get_read_conn() is not None
    db.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.get_session("s1")

    result = {}

    def later_reader():
        try:
            db.get_session("s1")
        except Exception as exc:
            result["exc"] = exc

    thread = threading.Thread(target=later_reader)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert isinstance(result.get("exc"), sqlite3.ProgrammingError)
    assert "closed database" in str(result["exc"])


@pytest.mark.requires_wal
def test_high_churn_reader_threads_return_all_connections_to_baseline(db):
    """Short-lived reader churn must not accumulate state.db file descriptors."""
    tracked_path = str(db.db_path.resolve())
    baseline_connections = _live_connections[tracked_path]

    def reader():
        assert db.get_session("s1")["id"] == "s1"

    for _ in range(175):
        thread = threading.Thread(target=reader)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert not db._read_conns
    assert _live_connections[tracked_path] == baseline_connections


@pytest.mark.requires_wal
def test_completed_reader_threads_close_their_connections(db):
    """Thread-local readers must close when their short-lived thread exits."""
    tracked_path = str(db.db_path.resolve())
    baseline_connections = _live_connections[tracked_path]

    def reader():
        assert db.get_session("s1")["id"] == "s1"

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not db._read_conns
    assert _live_connections[tracked_path] == baseline_connections


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
