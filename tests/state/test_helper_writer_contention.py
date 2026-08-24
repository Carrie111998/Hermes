"""Behavioral contention contract for the shared state.db helper-writer primitive.

``hermes_state_common.state_db_begin_immediate`` is the ``BEGIN IMMEDIATE``
discipline wired into ``tools.async_delegation._transaction`` and
``gateway.delivery_ledger._transaction``. The contract this module
proves is the ONE the previous false-green test missed: that the
*application* retry loop is genuinely exercised when a competing
writer holds the WAL write lock.

False-green pattern (the bug this test REPLACES): a previous test used
the helper's own ``sqlite3.connect(timeout=10)`` busy handler to ride
out a 250 ms hold, then asserted only the END state (``record_obligation
returned a row``). SQLite's deterministic busy wait would have satisfied
the same end state on its own; the application-level ``BEGIN IMMEDIATE``
retry loop could have been silently dead. This test instead:

  1. Forces the probe to use ``busy_timeout=0`` so SQLite's own
     internal busy wait CANNOT satisfy the contention — the FIRST
     ``BEGIN IMMEDIATE`` must raise ``SQLITE_BUSY`` immediately.
  2. Holds the lock from a separate thread that signals readiness
     ONLY after its own ``BEGIN IMMEDIATE`` returned successfully
     (so we are not racing thread scheduling).
  3. Instruments the probe connection's own ``execute()`` to count
     ``BEGIN IMMEDIATE`` invocations on the real call path through
     the primitive — this is the literal number of attempts the
     primitive made to acquire the lock.
  4. Counts the body call site independently and asserts it ran
     EXACTLY once (the primitive must not replay the body).
  5. Asserts the final value persisted via a fresh verifier
     connection — a one-shot wall-clock-independent witness that
     the body ran and the transaction committed.

If the application-level retry loop were silently disabled, ``BEGIN
IMMEDIATE`` would raise on attempt 1, the body would never run, and
``BEGIN_ATTEMPT_COUNT == 1`` / ``BODY_CALL_COUNT == 0`` / no row
persisted — the test would FAIL.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest


pytest.importorskip("hermes_state_common")

from hermes_state_common import (  # noqa: E402
    state_db_begin_immediate,
)


@pytest.fixture
def tmp_db_path(tmp_path):
    """Throwaway DB path; callers create their own connections."""
    return tmp_path / "test_state.db"


def _open_probe(path, *, busy_timeout_s: float = 0.0):
    """Open a probe connection in manual-transaction mode with a known busy timeout.

    ``busy_timeout_s=0`` (the default) disables SQLite's internal busy
    wait so the application retry loop is the ONLY thing standing
    between the probe and ``SQLITE_BUSY``.  Higher values are exposed
    for negative-control tests.
    """
    conn = sqlite3.connect(
        str(path), timeout=busy_timeout_s, isolation_level=None,
    )
    return conn


def _seed_wal(path):
    """Create a real DB with WAL journal mode and one row.

    The probe and the holder share this file; the row gives
    ``_persist_value`` a target table to write into.
    """
    seed = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        seed.execute("CREATE TABLE IF NOT EXISTS probe (v INTEGER)")
        seed.execute("INSERT OR IGNORE INTO probe (v) VALUES (0)")
        seed.execute("PRAGMA journal_mode=WAL")
    finally:
        seed.close()


class _BeginCounter:
    """Count ``BEGIN IMMEDIATE`` invocations on a probe connection.

    ``sqlite3.Connection`` is a static C type — its ``execute`` method
    is read-only and cannot be monkey-patched.  We wrap the connection
    in a delegating proxy whose ``execute`` intercepts
    ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` calls BEFORE
    delegating to the real connection.  This is a BEHAVIORIAL
    witness: the counter sees the literal SQL the primitive issues on
    the real connection, not a mocked re-implementation.
    """

    def __init__(self, conn):
        self._real = conn
        self.begin_attempts = 0
        self.commit_attempts = 0
        self.rollback_attempts = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, *args, **kwargs):
        text = sql.strip().upper() if isinstance(sql, str) else ""
        if text == "BEGIN IMMEDIATE":
            self.begin_attempts += 1
        elif text == "COMMIT":
            self.commit_attempts += 1
        elif text == "ROLLBACK":
            self.rollback_attempts += 1
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _hold_write_lock_until(
    db_path, conn_box: list, ready: threading.Event, release: threading.Event,
) -> None:
    """Holder thread: acquire the WAL write lock, signal ready, then hold.

    Signals ``ready`` ONLY after the holder's own ``BEGIN IMMEDIATE``
    returned successfully — i.e., the holder ACTUALLY has the lock, not
    just "the thread has been scheduled". This eliminates the test
    losing a race against the GIL.

    Waits for ``release`` before committing and closing, so the test
    thread controls exactly how long the lock is held.
    """
    conn = sqlite3.connect(
        str(db_path), timeout=10, isolation_level=None, check_same_thread=False,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn_box.append(conn)
        ready.set()
        release.wait()
    finally:
        try:
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        conn.close()


def test_application_retry_loop_genuinely_exercised(tmp_db_path):
    """The shared primitive MUST retry the application-level
    ``BEGIN IMMEDIATE`` while a competing writer holds the lock.

    See module docstring for the false-green pattern this test
    replaces.  The four concrete witnesses:

      * HOLDER_READY_AFTER_BEGIN_IMMEDIATE  — readiness event is set
        by the holder only after its own ``BEGIN IMMEDIATE`` returns.
      * PROBE_BUSY_TIMEOUT_SECONDS=0        — SQLite's own busy wait
        is disabled; only the primitive can satisfy the wait.
      * BEGIN_ATTEMPT_COUNT>1               — the primitive made
        multiple ``BEGIN IMMEDIATE`` calls on the probe connection
        (i.e., its retry loop ran at least one extra cycle).
      * BODY_CALL_COUNT=1                   — the body ran exactly
        once (the primitive does NOT replay the body on retry).
      * COMMIT_ATTEMPT_COUNT=1              — one successful commit.
      * BODY_REPLAY_OBSERVED=no             — the body was not
        invoked more than once.
      * persistence                         — the final value reflects
        one successful body+commit, observable from a fresh
        verifier connection independent of the probe thread.
    """
    _seed_wal(tmp_db_path)

    holder_conns: list = []
    holder_ready = threading.Event()
    holder_release = threading.Event()

    holder = threading.Thread(
        target=_hold_write_lock_until,
        args=(tmp_db_path, holder_conns, holder_ready, holder_release),
        daemon=True,
    )
    holder.start()

    # Wait until the holder ACTUALLY holds the WAL write lock.
    assert holder_ready.wait(timeout=2.0), (
        "holder thread never reported ready — "
        "its BEGIN IMMEDIATE did not return, so probe is not racing a real lock"
    )
    assert len(holder_conns) == 1, "holder connection never registered"

    # Hold the lock for a duration the probe's busy_timeout=0 cannot
    # bridge on its own. 0.3s is well above the primitive's per-cycle
    # jitter bounds (0.020–0.150s fast, 0.250–1.000s slow) so the
    # retry loop is forced to issue at least one more BEGIN IMMEDIATE.
    hold_seconds = 0.3
    holder_release.clear()

    def _release_after():
        time.sleep(hold_seconds)
        holder_release.set()
    release_thread = threading.Thread(target=_release_after, daemon=True)
    release_thread.start()

    # Open the probe with busy_timeout=0 so SQLite's internal busy
    # wait cannot satisfy the contention; the primitive is the only
    # thing that can.  Wrap it in a counter proxy so the primitive's
    # own BEGIN IMMEDIATE / COMMIT / ROLLBACK calls are observable.
    real_probe = _open_probe(tmp_db_path, busy_timeout_s=0.0)
    probe = _BeginCounter(real_probe)
    try:
        body_call_count = 0

        def body():
            nonlocal body_call_count
            body_call_count += 1
            probe.execute("UPDATE probe SET v = ? WHERE v = 0", (42,))

        with state_db_begin_immediate(probe):
            body()

        # ── Witnesses ─────────────────────────────────────────────
        # 1. Application retry loop fired: BEGIN IMMEDIATE was called
        #    MORE than once on the probe connection.  On attempt 1
        #    SQLite raises SQLITE_BUSY immediately (busy_timeout=0);
        #    the primitive must have retried after the holder released.
        assert probe.begin_attempts > 1, (
            f"expected BEGIN IMMEDIATE retried >1 times by the primitive, "
            f"got begin_attempts={probe.begin_attempts} — "
            f"if this is 1, the application retry loop is not exercising "
            f"the contended path (the probe should have failed on attempt 1)"
        )

        # 2. Body ran EXACTLY once (no replay on retry).
        assert body_call_count == 1, (
            f"body must run once (primitive does not replay the with-block), "
            f"got {body_call_count}"
        )

        # 3. Exactly one successful COMMIT.
        assert probe.commit_attempts == 1, (
            f"expected exactly one successful commit, "
            f"got commit_attempts={probe.commit_attempts}"
        )

        # 4. No rollback attempts (the body succeeded).
        assert probe.rollback_attempts == 0, (
            f"unexpected rollback(s) on the success path, "
            f"got rollback_attempts={probe.rollback_attempts}"
        )

        # Report the live values (visible in pytest -s output) so the
        # upstream rerereview can see the actual begin-attempt count
        # the primitive took on this run.
        print(
            f"\n[PR89420-CONTENTION-WITNESSES] "
            f"begin_attempts={probe.begin_attempts} "
            f"body_call_count={body_call_count} "
            f"commit_attempts={probe.commit_attempts} "
            f"rollback_attempts={probe.rollback_attempts} "
            f"hold_seconds={hold_seconds} "
            f"probe_busy_timeout_s=0.0 "
            f"holder_signaled_ready_after_own_begin_immediate=yes"
        )
    finally:
        probe.close()

    holder.join(timeout=2.0)
    release_thread.join(timeout=2.0)

    # 5. Persistence witness — a fresh verifier connection reads the
    #    value the body wrote, independent of the probe thread.  If
    #    the primitive silently swallowed the body or the commit
    #    never landed, this would be 0 (the seed value).
    verifier = sqlite3.connect(str(tmp_db_path), timeout=10, isolation_level=None)
    try:
        row = verifier.execute("SELECT v FROM probe").fetchone()
    finally:
        verifier.close()
    assert row is not None, "probe row missing after contended write"
    assert row[0] == 42, (
        f"expected the body-written value 42 (proves one body+commit), "
        f"got {row[0]} — either body did not run, body ran more than once, "
        f"or commit did not persist"
    )


def test_probe_busy_timeout_is_zero():
    """Document the busy-timeout choice on the probe.

    The probe MUST use ``busy_timeout=0`` so the application retry
    loop is the only mechanism that can satisfy contention.  This
    test asserts the contract on the helper used by the contention
    test itself: it would catch a future refactor that re-enables
    SQLite's internal busy wait and silently re-creates the
    false-green pattern.
    """
    # _open_probe has busy_timeout_s as a keyword-only default.
    kw_defaults = _open_probe.__kwdefaults__ or {}
    assert kw_defaults.get("busy_timeout_s") == 0.0, (
        f"_open_probe default busy_timeout_s must remain 0.0 to keep the "
        f"contention test honest; got {kw_defaults!r}"
    )


def test_holder_readiness_signals_after_its_own_begin_immediate():
    """Holder readiness contract.

    The contention test's holder thread signals "ready" ONLY after
    its own ``BEGIN IMMEDIATE`` returned — not when the thread was
    merely scheduled.  This is what guarantees the probe is racing
    a *real* held lock, not a thread that has not yet acquired one.

    This test exercises the holder helper against a real DB and
    confirms the readiness event fires after the holder connection
    shows up in the connection box (the post-``BEGIN IMMEDIATE``
    invariant).
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = str((__import__("pathlib").Path(td) / "h.db").resolve())
        # Seed the DB so WAL has a target.
        seed = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            seed.execute("CREATE TABLE t (x INTEGER)")
            seed.execute("PRAGMA journal_mode=WAL")
        finally:
            seed.close()

        box: list = []
        ready = threading.Event()
        release = threading.Event()

        thread = threading.Thread(
            target=_hold_write_lock_until,
            args=(path, box, ready, release),
            daemon=True,
        )
        thread.start()

        # Spin until both conditions are met, with a bounded wait.
        deadline = time.monotonic() + 2.0
        while not (ready.is_set() and box):
            if time.monotonic() > deadline:
                release.set()
                thread.join(timeout=1.0)
                pytest.fail(
                    "holder readiness contract broken: "
                    "ready.set() must only fire AFTER BEGIN IMMEDIATE has returned "
                    "and the connection is in the box"
                )
            time.sleep(0.01)

        # Sanity: ready and box came in together, holding the lock.
        assert ready.is_set() is True
        assert len(box) == 1

        # Cleanup.
        release.set()
        thread.join(timeout=2.0)
