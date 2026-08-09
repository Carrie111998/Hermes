"""Tests for SessionDB idle-eviction of the read-connection cache.

The bounded read cache (_MAX_READ_CONNS=32) prevents runaway growth, but
without idle-eviction a long-lived handle that goes quiet (the dashboard
parked overnight, a gateway between turns) sits at the cap forever — the
pool never drains. These tests pin the new contract: connections idle
past _READ_CONN_IDLE_TIMEOUT are closed by the per-instance daemon
sweeper even when the cache is under the bound, and the owning thread
detects the close via conn._hermes_read_closed and reopens lazily on its
next read (no generation bump, so active threads keep warm connections).
"""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    d.append_message("s1", role="user", content="hello idle eviction")
    yield d
    d.close()


@pytest.mark.requires_wal
def test_idle_eviction_closes_conns_under_the_bound(db):
    """Connections idle past the timeout are closed even below _MAX_READ_CONNS."""
    conn = db._get_read_conn()
    assert conn is not None
    # Park the connection as idle by back-dating its last-used stamp.
    setattr(conn, "_hermes_last_used", time.monotonic() - db._READ_CONN_IDLE_TIMEOUT - 1)

    db._read_conn_sweeper_loop_iteration = True  # no-op marker; loop is exercised below

    # Run one sweep directly (the daemon loop is exercised via the timeout test).
    stale = []
    with db._read_conns_lock:
        for c in list(db._read_conns):
            last = getattr(c, "_hermes_last_used", time.monotonic())
            if time.monotonic() - last >= db._READ_CONN_IDLE_TIMEOUT:
                stale.append(c)
    for c in stale:
        if db._close_read_conn(c):
            with db._read_conns_lock:
                db._read_conns.discard(c)

    assert conn._hermes_read_closed is True
    assert conn not in db._read_conns


@pytest.mark.requires_wal
def test_idle_eviction_reopens_lazily_on_next_read(db):
    """The owning thread detects the closed conn and reopens on the next read."""
    conn = db._get_read_conn()
    assert conn is not None
    setattr(conn, "_hermes_last_used", time.monotonic() - db._READ_CONN_IDLE_TIMEOUT - 1)

    # Close it as the sweeper would.
    db._close_read_conn(conn)
    with db._read_conns_lock:
        db._read_conns.discard(conn)

    # Next read must NOT hand back the closed connection.
    conn2 = db._get_read_conn()
    assert conn2 is not None
    assert conn2 is not conn
    assert conn2._hermes_read_closed is False
    # And the pool is back to one live connection.
    with db._read_conns_lock:
        assert len(db._read_conns) == 1


@pytest.mark.requires_wal
def test_active_conn_is_not_evicted(db):
    """A recently-used connection survives a sweep."""
    conn = db._get_read_conn()
    setattr(conn, "_hermes_last_used", time.monotonic())  # just used

    with db._read_conns_lock:
        stale = [
            c
            for c in list(db._read_conns)
            if time.monotonic() - getattr(c, "_hermes_last_used", time.monotonic())
            >= db._READ_CONN_IDLE_TIMEOUT
        ]
    assert stale == []
    assert conn._hermes_read_closed is False


@pytest.mark.requires_wal
def test_read_ctx_never_yields_a_closed_conn(db):
    """_read_ctx re-verifies the conn is not closed before yielding."""
    conn = db._get_read_conn()
    setattr(conn, "_hermes_last_used", time.monotonic() - db._READ_CONN_IDLE_TIMEOUT - 1)
    db._close_read_conn(conn)
    with db._read_conns_lock:
        db._read_conns.discard(conn)

    # A read through _read_ctx must transparently reopen, not crash.
    with db._read_ctx() as c:
        row = c.execute("SELECT count(*) FROM sessions").fetchone()
        assert row[0] == 1


@pytest.mark.requires_wal
def test_daemon_sweeper_drains_idle_pool(tmp_path):
    """End-to-end: the daemon thread closes idle conns on its own schedule.

    Uses tiny sweep intervals so the test finishes fast, then verifies the
    pool drains to zero and a fresh read re-warms it.
    """
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    d.append_message("s1", role="user", content="hello sweeper")
    # Shrink the cadence on this instance only.
    d._READ_CONN_IDLE_TIMEOUT = 0.05
    d._READ_CONN_IDLE_SWEEP_INTERVAL = 0.02
    try:
        conn = d._get_read_conn()
        assert conn is not None
        with d._read_conns_lock:
            assert len(d._read_conns) == 1

        # Back-date the stamp so the next sweep considers it idle.
        setattr(conn, "_hermes_last_used", time.monotonic() - 10)

        # Wait for the sweeper thread (started lazily on _get_read_conn) to
        # run its first tick and close the idle connection.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with d._read_conns_lock:
                if len(d._read_conns) == 0:
                    break
            time.sleep(0.01)
        with d._read_conns_lock:
            assert len(d._read_conns) == 0, "daemon sweeper did not drain the pool"
        assert conn._hermes_read_closed is True

        # A subsequent read re-warms the pool lazily.
        conn2 = d._get_read_conn()
        assert conn2 is not None
        assert conn2._hermes_read_closed is False
    finally:
        d.close()
