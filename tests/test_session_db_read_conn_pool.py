"""The SessionDB read path must not leak one connection per (SessionDB x thread).

``_get_read_conn`` used to cache a read-only connection in ``threading.local()``
and pin it in a strong set (``_read_conns``) that was only ever drained by
``close()``. Starlette dispatches sync routes on anyio worker threads, so a
SessionDB that is never closed -- the dashboard's module-global ``_db`` and
the per-session ``session_db`` handles -- gained a connection, and a file
descriptor, for every worker thread that ever served a read. In production
that walked into the 256 soft ``RLIMIT_NOFILE`` a service manager hands the
process, after which every request failed with ``OSError`` EMFILE while the
process stayed alive, so the supervisor's restart-on-exit never fired.

Worse, those connections were opened WITHOUT ``check_same_thread=False`` (both
writer opens pass it), so ``close()`` on them raised ``ProgrammingError`` from
a different thread and the bare ``except Exception: pass`` hid it -- leaving
``hermes_cli.sqlite_safe_read``'s registry permanently over-counted as well.

The contract pinned here: reads borrow from a BOUNDED pool, connections are
returned and reused, surplus connections are closed rather than dropped, and
``close()`` actually closes them from whatever thread it runs on.

Bounded means bounded at PEAK, not merely at rest. Pooling returns behind a
``maxsize`` LifoQueue while opening unconditionally on a miss still lets a
burst of N simultaneous readers on a cold pool open N descriptors before
closing the surplus -- which is the exact shape of the production incident,
since the burst that exhausts the pool is the burst that exhausts the fd
table. Peak is held down by a permit acquired before the open and released
after the close; past the ceiling readers degrade to the locked writer
connection. Tests that join their workers before counting cannot see any of
this, so the peak assertions use a barrier.

These assert on the pool/registry counts, never on ``lsof``: SQLite's unix VFS
parks a closed descriptor on a per-inode reuse list while any connection still
holds POSIX locks on that inode, so raw descriptor counts lag the real
connection count and make such assertions flaky.
"""

import sqlite3
import threading

import pytest

from hermes_state import SessionDB


def _live_count(path) -> int:
    """Live-connection count the tracking registry holds for *path*."""
    import hermes_cli.sqlite_safe_read as mod

    with mod._live_lock:
        return mod._live_connections.get(mod._key(path), 0)


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.create_session(session_id="s1", source="cli", model="m")
    d.append_message("s1", role="user", content="hello graphiti world")
    d.append_message("s1", role="assistant", content="the neo4j daemon is healthy")
    yield d
    d.close()


def _read(db):
    db.get_session("s1")
    db.search_messages("graphiti", limit=5)
    db.get_messages("s1")


@pytest.mark.requires_wal
def test_read_pool_is_bounded_across_many_threads(db):
    """150 short-lived reader threads must not pin 150 connections.

    NOTE: this measures the pool AT REST -- every worker is joined before the
    count is taken, so by construction it cannot observe how many connections
    were open simultaneously. It is a real assertion about accumulation and a
    non-assertion about peak. See
    test_peak_live_connections_bounded_under_simultaneous_burst for the peak.
    """
    maxsize = db._read_pool.maxsize
    assert maxsize > 0, "read pool must be bounded"

    for _ in range(6):
        threads = [threading.Thread(target=_read, args=(db,)) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert db._read_pool.qsize() <= maxsize

    # The pre-fix code held 151 connections here (150 readers + main thread).
    assert db._read_pool.qsize() <= maxsize
    # +1 for the writer connection SessionDB always holds.
    assert _live_count(db.db_path) <= maxsize + 1


@pytest.mark.requires_wal
def test_read_conn_returned_to_pool_and_reused(db):
    """Sequential reads on one thread reuse a pooled connection, not a new one."""
    with db._read_ctx() as conn:
        first = conn
    assert db._read_pool.qsize() >= 1, "connection was not returned to the pool"
    with db._read_ctx() as conn:
        assert conn is first, "pooled connection was not reused"


def _poison(db, exc: BaseException) -> sqlite3.Connection:
    """Borrow the pooled connection and fail its block with *exc*.

    Returns the connection that was checked out, so callers can assert on its
    identity afterwards. The failure is raised from inside the ``_read_ctx``
    block because that is exactly what a query failure looks like to the
    context manager -- and corrupting the backing file instead is NOT
    deterministic here: SQLite serves the already-open connection from its
    page cache, so a truncated state.db keeps returning rows until the cache
    is evicted.
    """
    with pytest.raises(type(exc)):
        with db._read_ctx() as conn:
            borrowed = conn
            raise exc
    return borrowed


@pytest.mark.requires_wal
def test_pooled_read_conn_discarded_after_database_error(db):
    """A pooled connection whose query raised DatabaseError must not come back.

    The write path already self-heals; the read pool did not. Returning the
    connection unconditionally meant a poisoned one -- truncated backing file,
    "file is not a database", a state.db replaced under a long-lived gateway --
    went to the head of a LifoQueue and was handed straight back to the next
    checkout, so every later read failed for as long as the process lived.
    """
    with db._read_ctx() as conn:
        primed = conn
    assert db._read_pool.qsize() >= 1, "pool must be primed for this to test anything"

    poisoned = _poison(db, sqlite3.DatabaseError("file is not a database"))
    assert poisoned is primed, "LIFO must have handed back the pooled connection"

    assert poisoned not in list(db._read_pool.queue), (
        "poisoned connection was returned to the pool"
    )
    with db._read_ctx() as conn:
        assert conn is not poisoned, "next checkout reused the poisoned connection"


@pytest.mark.requires_wal
def test_read_path_self_heals_after_database_error(db):
    """The read after the discard must serve real rows off a fresh connection."""
    with db._read_ctx() as conn:
        primed = conn
    poisoned = _poison(db, sqlite3.DatabaseError("database disk image is malformed"))
    assert poisoned is primed

    assert db.get_session("s1")["id"] == "s1"
    assert len(db.get_messages("s1")) == 2
    assert db.search_messages("graphiti", limit=5)


@pytest.mark.requires_wal
def test_discarded_read_conn_returns_its_permit(db):
    """Discarding must go through _close_read_conn, not drop the connection.

    A discard that skipped the permit release would trade a poisoned
    connection for a permanently narrower read path -- the ceiling ratcheting
    down one slot per fault, which is the same outage in slow motion.
    """
    from hermes_state import _READ_POOL_MAX

    with db._read_ctx():
        pass
    for _ in range(_READ_POOL_MAX * 2):
        _poison(db, sqlite3.DatabaseError("file is not a database"))

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    try:
        assert all(c is not None for c in held), (
            "discards stranded permits -- the ceiling ratcheted down"
        )
    finally:
        for c in held:
            if c is not None:
                db._close_read_conn(c)


@pytest.mark.requires_wal
def test_non_database_error_keeps_the_pooled_conn(db):
    """The discard is scoped to DatabaseError, not to any failure in the block.

    Caller-side bugs (a KeyError while shaping rows, a cancellation) say
    nothing about the connection's health, and discarding on those would throw
    away a good descriptor -- and the pool's whole point -- on every unrelated
    exception.
    """
    with db._read_ctx() as conn:
        primed = conn

    borrowed = _poison(db, ValueError("caller-side bug"))
    assert borrowed is primed
    assert borrowed in list(db._read_pool.queue), (
        "a non-DatabaseError must leave the connection in the pool"
    )
    with db._read_ctx() as conn:
        assert conn is primed, "healthy connection must still be reused"


@pytest.mark.requires_wal
def test_pooled_conn_is_usable_from_another_thread(db):
    """A pooled connection is handed between threads, so it must not be
    bound to its creating thread (check_same_thread=False)."""
    with db._read_ctx() as conn:
        borrowed = conn

    errors = []

    def use_it():
        try:
            borrowed.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=use_it)
    t.start()
    t.join()
    assert not errors, f"pooled connection unusable off-thread: {errors}"


@pytest.mark.requires_wal
def test_close_drains_pool_from_a_foreign_thread(tmp_path):
    """close() must actually close pooled connections, including ones opened
    on threads that have since exited -- the swallowed ProgrammingError."""
    d = SessionDB(db_path=tmp_path / "state2.db")
    d.create_session(session_id="s1", source="cli", model="m")

    # Populate the pool from a worker thread, then let that thread die.
    t = threading.Thread(target=lambda: d.get_session("s1"))
    t.start()
    t.join()
    assert d._read_pool.qsize() >= 1

    d.close()
    assert d._read_pool.qsize() == 0
    # Registry back to zero proves the closes succeeded rather than raising
    # ProgrammingError into a bare except.
    assert _live_count(d.db_path) == 0


@pytest.mark.requires_wal
def test_reader_after_close_does_not_repopulate_pool(db):
    """A read racing close() must close its connection, not refill the pool."""
    db.close()
    assert db._read_pool.qsize() == 0
    # A read arriving after the drain must not open-and-requeue a connection
    # that nothing will ever close again.
    with db._read_ctx():
        pass
    assert db._read_pool.qsize() == 0


def test_reads_are_still_correct_under_concurrency(db):
    """Pooling must not corrupt results when threads share connections."""
    results = []
    errors = []

    def reader():
        try:
            results.append(db.get_session("s1")["id"])
            results.append(len(db.get_messages("s1")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent reads failed: {errors}"
    assert results.count("s1") == 12
    assert results.count(2) == 12


@pytest.mark.requires_wal
def test_read_open_failure_backs_off_but_recovers(db):
    """A failed read-only open must not permanently demote the read path.

    The first version of this fix used a sticky instance-wide boolean
    (``_read_open_failed``). Its likeliest trigger is transient fd pressure --
    EMFILE, the very condition this pool exists to prevent -- and because the
    gateway shares ONE SessionDB across every agent, a single blip would have
    convoyed every subsequent reader behind the writer lock for the life of
    the process. The stamp must expire.
    """
    import time as _time

    from hermes_state import _READ_OPEN_RETRY_SECONDS

    baseline = db._get_read_conn()
    assert baseline is not None, "baseline read open should succeed"
    db._close_read_conn(baseline)

    db._read_open_failed_at = _time.monotonic()
    assert db._get_read_conn() is None, "should back off immediately after a failure"

    db._read_open_failed_at = _time.monotonic() - (_READ_OPEN_RETRY_SECONDS + 1)
    recovered = db._get_read_conn()
    assert recovered is not None, "read path must self-heal once the window expires"
    db._close_read_conn(recovered)


@pytest.mark.requires_wal
def test_checkout_seam_is_the_single_acquisition_point(db):
    """``_read_ctx`` must acquire via ``_checkout_read_conn`` and nothing else.

    If a future edit re-inlines the pool checkout into ``_read_ctx``, patching
    ``_get_read_conn`` silently exercises nothing whenever the pool is warm --
    which is exactly how the writer-lock fallback test below would rot into a
    no-op without failing.
    """
    calls = []
    original = db._checkout_read_conn

    def _spy():
        calls.append(1)
        return original()

    db._checkout_read_conn = _spy
    try:
        with db._read_ctx():
            pass
    finally:
        db._checkout_read_conn = original
    assert calls, "_read_ctx must route acquisition through _checkout_read_conn"


def test_fallback_to_locked_writer_when_read_conn_unavailable(db, monkeypatch):
    """With no read connection available, reads still work under self._lock.

    Patched at the acquisition SEAM rather than at ``_get_read_conn``: the
    pool is consulted first, so a patched ``_get_read_conn`` is never reached
    while the pool holds a connection and this test would pass while
    exercising nothing.
    """
    monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
    assert db.get_session("s1")["id"] == "s1"
    assert db.search_messages("graphiti", limit=5)


@pytest.mark.requires_wal
def test_peak_live_connections_bounded_under_simultaneous_burst(db):
    """N readers checked out AT THE SAME INSTANT must not open N connections.

    This is the assertion the join-then-count test above cannot make. A
    LifoQueue with a maxsize bounds how many connections are RETURNED, not how
    many are OPEN: with an open-on-miss checkout, 64 readers arriving on a cold
    pool opened 64 descriptors and only then closed 56 of them on release.
    Bounded at rest, unbounded at peak -- and EMFILE is a peak-instant
    condition, so the process could still wedge exactly as it did in
    production.

    The barrier is the whole point: every worker holds its connection until all
    of them have checked out, so the count below IS the simultaneous peak
    rather than a sample of it.
    """
    from hermes_state import _READ_POOL_MAX

    n = 64
    assert n > _READ_POOL_MAX, "burst must exceed the ceiling to test anything"

    ready = threading.Barrier(n + 1)
    release = threading.Event()
    checked_out = []
    fell_back = []
    lock = threading.Lock()

    def worker():
        conn = db._checkout_read_conn()
        with lock:
            (checked_out if conn is not None else fell_back).append(conn)
        ready.wait(timeout=30)      # everyone is now holding whatever they got
        release.wait(timeout=30)
        if conn is not None:
            db._close_read_conn(conn)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()

    ready.wait(timeout=30)
    # ---- the instant every worker is simultaneously checked out ----
    peak_live = _live_count(db.db_path)
    peak_checked_out = len(checked_out)
    release.set()
    for t in threads:
        t.join(timeout=30)

    assert peak_checked_out <= _READ_POOL_MAX, (
        f"{peak_checked_out} connections checked out at once; the ceiling is "
        f"{_READ_POOL_MAX}. Peak is unbounded -- the pool bounds returns, not opens."
    )
    # +1 for the writer connection SessionDB always holds.
    assert peak_live <= _READ_POOL_MAX + 1, (
        f"{peak_live} live connections at peak, ceiling is {_READ_POOL_MAX} (+1 writer)"
    )
    assert fell_back, "with n > ceiling some readers must degrade to the writer path"
    assert len(checked_out) + len(fell_back) == n, "every worker must be accounted for"


@pytest.mark.requires_wal
def test_exhausted_permits_fall_back_to_the_writer_connection(db):
    """Past the ceiling the read path degrades, it does not fail or block.

    A reader that cannot get a permit must serve from the locked writer
    connection. Blocking instead would convert descriptor exhaustion into a
    stall -- the same outage with a different stack trace.
    """
    from hermes_state import _READ_POOL_MAX

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    assert all(c is not None for c in held), "the first _READ_POOL_MAX must succeed"
    try:
        assert db._checkout_read_conn() is None, "ceiling must refuse the next open"
        with db._read_ctx() as conn:
            assert conn is db._conn, "must fall back to the shared writer connection"
            assert conn.execute("SELECT 1").fetchone()[0] == 1, "fallback must work"
    finally:
        for c in held:
            db._close_read_conn(c)

    # Permits come back: the read path recovers once the burst drains.
    recovered = db._checkout_read_conn()
    assert recovered is not None, "permits must be released back after close"
    db._close_read_conn(recovered)


@pytest.mark.requires_wal
def test_permits_are_not_stranded_by_a_failed_open(db, monkeypatch):
    """A failed open must return its permit, or the ceiling ratchets to zero.

    A permit leaked per failure is not a transient error: it permanently
    shrinks the read path, so a burst of transient open failures would silently
    demote every later read to the writer lock for the life of the process.
    """
    import sqlite3 as _sqlite3

    import hermes_state as _hs
    from hermes_state import _READ_POOL_MAX

    def boom(*a, **kw):
        raise _sqlite3.OperationalError("simulated open failure")

    monkeypatch.setattr(_hs, "_connect_tracked_db", boom)
    for _ in range(_READ_POOL_MAX * 3):
        assert db._get_read_conn() is None
        db._read_open_failed_at = 0.0    # defeat the backoff so every call opens
    monkeypatch.undo()

    db._read_open_failed_at = 0.0
    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    try:
        assert all(c is not None for c in held), (
            "permits were stranded by failed opens -- the ceiling ratcheted down"
        )
    finally:
        for c in held:
            if c is not None:
                db._close_read_conn(c)


@pytest.mark.requires_wal
def test_close_returns_every_permit(db):
    """close() must release the permits its drained connections held."""
    from hermes_state import _READ_POOL_MAX

    held = [db._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    for c in held:
        db._read_pool.put_nowait(c)
    assert db._read_pool.qsize() == _READ_POOL_MAX

    db.close()
    assert db._read_pool.qsize() == 0
    assert _live_count(db.db_path) == 0
    # BoundedSemaphore raises on over-release, so draining exactly
    # _READ_POOL_MAX permits proves close() released neither too few nor too
    # many.
    for _ in range(_READ_POOL_MAX):
        assert db._read_permits.acquire(blocking=False), "close() stranded a permit"
    assert not db._read_permits.acquire(blocking=False), "close() over-released"
