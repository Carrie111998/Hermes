"""Storage-layer conformance for the cross-process turn lease (#67442).

``gateway/turn_lease.py``'s in-process registry cannot see a CLI process and a
gateway process sharing one session_id through CLI-continuity — the route that
produced the #64934 transcript interleaving. These tests pin the DB-level
contract the cross-process lease depends on:

* acquire is atomic — exactly one of N racing callers wins,
* a crashed holder is reclaimed by expiry rather than wedging the session
  forever,
* release is holder-scoped, so a late-returning turn cannot delete a lease
  somebody else now holds,
* refresh reports lost ownership instead of silently succeeding.

Contention is exercised with real threads against one SQLite file rather than
mocks: the whole point of this layer is what happens when two writers race, and
a mocked connection would prove nothing about it.
"""

import sqlite3
import threading
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def state(tmp_path):
    st = SessionDB(tmp_path / "state.db")
    yield st
    try:
        st.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------------


def test_acquire_grants_the_lease(state):
    assert state.try_acquire_turn_lease("s1", "cli:1", surface="cli") is True
    assert state.get_turn_lease_holder("s1") == "cli:1"


def test_second_holder_is_refused_while_lease_is_live(state):
    assert state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60) is True
    assert state.try_acquire_turn_lease("s1", "gateway:2", ttl_seconds=60) is False
    assert state.get_turn_lease_holder("s1") == "cli:1"


def test_same_holder_reacquire_is_idempotent(state):
    """Retry paths must not deadlock against their own row."""
    assert state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60) is True
    assert state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60) is True
    assert state.get_turn_lease_holder("s1") == "cli:1"


def test_leases_are_per_session(state):
    assert state.try_acquire_turn_lease("s1", "cli:1") is True
    assert state.try_acquire_turn_lease("s2", "cli:1") is True
    assert state.get_turn_lease_holder("s1") == "cli:1"
    assert state.get_turn_lease_holder("s2") == "cli:1"


def test_blank_session_or_holder_never_acquires(state):
    assert state.try_acquire_turn_lease("", "cli:1") is False
    assert state.try_acquire_turn_lease("s1", "") is False


def test_concurrent_acquire_grants_exactly_one_winner(tmp_path):
    """The invariant the whole feature rests on, raced at the SQLite layer.

    Each thread opens its OWN ``SessionDB`` on the same file. That detail is
    the test: ``_execute_write`` wraps its whole BEGIN IMMEDIATE transaction in
    ``self._lock``, a *per-instance* ``threading.Lock`` (hermes_state.py:2543,
    3311). Racing 8 threads through one shared instance would prove only that
    that mutex works — the writers would be ordered in Python and never
    contend in SQLite at all. Separate instances mean separate locks and
    separate connections, which is the shape of the case this table exists
    for: two OS processes sharing one state.db.
    """
    db_path = tmp_path / "state.db"
    SessionDB(db_path).close()  # create the schema once, up front

    winners = []
    guard = threading.Lock()
    start = threading.Barrier(8)

    def contend(i):
        db = SessionDB(db_path)
        try:
            start.wait()
            if db.try_acquire_turn_lease("shared", f"holder:{i}", ttl_seconds=60):
                with guard:
                    winners.append(f"holder:{i}")
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"

    verify = SessionDB(db_path)
    try:
        assert verify.get_turn_lease_holder("shared") == winners[0]
    finally:
        verify.close()


def test_second_process_sees_the_first_holders_lease(tmp_path):
    """A lease taken on one connection is visible/blocking on another.

    The cross-process contract in one assertion: without shared visibility
    through the DB row, the CLI-continuity pair behind #64934 would each
    believe they hold the session.
    """
    db_path = tmp_path / "state.db"
    cli = SessionDB(db_path)
    gateway = SessionDB(db_path)
    try:
        assert cli.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60) is True
        # Different instance, different connection, different lock:
        assert gateway.try_acquire_turn_lease("s1", "gw:2", ttl_seconds=60) is False
        assert gateway.get_turn_lease_holder("s1") == "cli:1"

        cli.release_turn_lease("s1", "cli:1")
        assert gateway.try_acquire_turn_lease("s1", "gw:2", ttl_seconds=60) is True
    finally:
        cli.close()
        gateway.close()


# ---------------------------------------------------------------------------
# Expiry reclamation — a crashed holder must not wedge the session
# ---------------------------------------------------------------------------


def test_expired_lease_is_reclaimed_by_the_next_acquirer(state):
    assert state.try_acquire_turn_lease("s1", "crashed", ttl_seconds=0.05) is True
    time.sleep(0.1)
    assert state.try_acquire_turn_lease("s1", "next", ttl_seconds=60) is True
    assert state.get_turn_lease_holder("s1") == "next"


def test_expired_lease_reports_no_holder(state):
    state.try_acquire_turn_lease("s1", "crashed", ttl_seconds=0.05)
    time.sleep(0.1)
    assert state.get_turn_lease_holder("s1") is None


def test_holder_is_none_for_unknown_session(state):
    assert state.get_turn_lease_holder("never-seen") is None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def test_refresh_extends_a_held_lease(state):
    assert state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=0.2) is True
    assert state.refresh_turn_lease("s1", "cli:1", ttl_seconds=60) is True
    time.sleep(0.25)
    # Would have expired on the original TTL; the refresh moved it out.
    assert state.get_turn_lease_holder("s1") == "cli:1"
    assert state.try_acquire_turn_lease("s1", "other", ttl_seconds=60) is False


def test_refresh_fails_for_a_non_holder(state):
    state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60)
    assert state.refresh_turn_lease("s1", "impostor", ttl_seconds=60) is False


def test_refresh_fails_after_the_lease_was_reclaimed(state):
    """The signal a long turn needs: ownership was lost mid-flight."""
    state.try_acquire_turn_lease("s1", "slow", ttl_seconds=0.05)
    time.sleep(0.1)
    state.try_acquire_turn_lease("s1", "next", ttl_seconds=60)
    assert state.refresh_turn_lease("s1", "slow", ttl_seconds=60) is False


def test_refresh_of_unknown_session_is_false(state):
    assert state.refresh_turn_lease("nope", "cli:1") is False


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_frees_the_lease(state):
    state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60)
    state.release_turn_lease("s1", "cli:1")
    assert state.get_turn_lease_holder("s1") is None
    assert state.try_acquire_turn_lease("s1", "other", ttl_seconds=60) is True


def test_release_by_a_non_holder_is_a_no_op(state):
    """A late-returning turn must not free a lease someone else now holds."""
    state.try_acquire_turn_lease("s1", "current", ttl_seconds=60)
    state.release_turn_lease("s1", "stale-loser")
    assert state.get_turn_lease_holder("s1") == "current"


def test_release_is_idempotent(state):
    state.try_acquire_turn_lease("s1", "cli:1", ttl_seconds=60)
    state.release_turn_lease("s1", "cli:1")
    state.release_turn_lease("s1", "cli:1")  # must not raise
    assert state.get_turn_lease_holder("s1") is None


def test_generation_scoped_release_ignores_a_stale_attempt(state):
    """One holder id, two acquisitions: the retried run must not release the live one."""
    state.try_acquire_turn_lease("s1", "cli:1", run_generation=1, ttl_seconds=0.05)
    time.sleep(0.1)
    state.try_acquire_turn_lease("s1", "cli:1", run_generation=2, ttl_seconds=60)

    state.release_turn_lease("s1", "cli:1", run_generation=1)  # stale
    assert state.get_turn_lease_holder("s1") == "cli:1"

    state.release_turn_lease("s1", "cli:1", run_generation=2)  # live
    assert state.get_turn_lease_holder("s1") is None


# ---------------------------------------------------------------------------
# Degradation — the lease subsystem must never wedge a turn
# ---------------------------------------------------------------------------


def test_acquire_returns_false_when_the_store_errors(state, monkeypatch):
    """Unusable lease store => exclusivity not proven, never an exception."""

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(state, "_execute_write", _boom)
    assert state.try_acquire_turn_lease("s1", "cli:1") is False
    assert state.refresh_turn_lease("s1", "cli:1") is False
    state.release_turn_lease("s1", "cli:1")  # must not raise


def test_surface_and_generation_round_trip(state):
    """Recorded for diagnostics and fencing; assert they actually persist."""
    state.try_acquire_turn_lease(
        "s1", "cli:1", surface="cli", run_generation=7, ttl_seconds=60
    )
    row = state._conn.execute(
        "SELECT surface, run_generation FROM turn_leases WHERE session_id = ?",
        ("s1",),
    ).fetchone()
    assert row["surface"] == "cli"
    assert row["run_generation"] == 7
