"""Phase 1 RC2 — typed refresh_compression_lock + refresher liveness.

Real SessionDB tests for the typed Literal["renewed","transient_failure","ownership_lost"]
state machine. Uses real SQLite (tmp_path) not FlakyDB False-collapse.
"""
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from agent.conversation_compression import _CompressionLockLeaseRefresher
from hermes_state import SessionDB, CompressionSessionBusyError


def _setup_db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_lock(db, session_id, holder, expired=False, ttl=300.0):
    now = time.time()
    expires = now - 10.0 if expired else now + ttl
    db._conn.execute(
        "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, holder, now, expires),
    )
    db._conn.commit()


# ---------------------------------------------------------------------------
# 1. successful renewal
# ---------------------------------------------------------------------------
def test_refresh_renewed_extends_lease(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("s1", source="test")
    assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True
    before = db._conn.execute("SELECT expires_at FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()[0]
    time.sleep(0.02)
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) == "renewed"
    after = db._conn.execute("SELECT expires_at FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()[0]
    assert after > before


# ---------------------------------------------------------------------------
# 2. transient failure then renewal (simulated via _execute_write raising)
# ---------------------------------------------------------------------------
def test_transient_failure_then_renewed(tmp_path, monkeypatch):
    db = _setup_db(tmp_path)
    db.create_session("s1", source="test")
    assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=300.0) is True

    call = {"n": 0}
    orig_execute = db._execute_write

    def flaky_execute(fn, patience_s=None):
        call["n"] += 1
        if call["n"] == 1:
            # Simulate full patience exhaustion — locked for 20s
            raise sqlite3.OperationalError("database is locked")
        return orig_execute(fn, patience_s=patience_s)

    with patch.object(db, "_execute_write", side_effect=flaky_execute):
        # First call hits contention -> transient_failure
        assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) == "transient_failure"

    # Next call succeeds -> renewed, holder still owns
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) == "renewed"


# ---------------------------------------------------------------------------
# 3. contention lasting longer than one interval (multiple transients then renew)
#     — genuinely exercises _CompressionLockLeaseRefresher._run()
# ---------------------------------------------------------------------------
def test_transient_burst_then_recovery(tmp_path):
    db = _setup_db(tmp_path)
    ttl, interval = 10.0, 0.05  # fast, cap = 200
    db.create_session("s1", source="test")
    assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=ttl) is True

    from agent.conversation_compression import _CompressionLockLeaseRefresher as Refresher

    # Simulate 7 consecutive transient failures then renewed; refresher must survive >5
    results = ["transient_failure"] * 7 + ["renewed"]
    idx = {"i": 0}
    refresher_ref = {}

    def fake_refresh(sid, holder, ttl_seconds=300.0):
        r = results[idx["i"]] if idx["i"] < len(results) else "renewed"
        idx["i"] += 1
        if idx["i"] >= 8:
            # Stop after 8 ticks (7 transient + 1 renewed) — proves survival beyond cap=5
            refresher_ref["ref"]._stop.set()
        return r

    db_mock = type("DB", (), {"refresh_compression_lock": staticmethod(fake_refresh)})()
    refresher = Refresher(db_mock, "s1", "holder-a", ttl_seconds=ttl, refresh_interval_seconds=interval)
    refresher_ref["ref"] = refresher
    # _run uses _stop.wait(interval); with interval=0.05 it's already fast (~0.4s total)
    refresher._run()
    assert idx["i"] >= 8, "refresher must have survived 7 transients and reached renewed"
    # Verify real DB holder still owns row after burst (control)
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=ttl) == "renewed"


# ---------------------------------------------------------------------------
# 4. genuine holder replacement
# ---------------------------------------------------------------------------
def test_genuine_holder_replacement(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("s1", source="test")
    import hermes_state
    # holder-a acquires
    orig_time = time.time
    base = 1000.0
    with patch.object(hermes_state.time, "time", lambda: base):
        assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True
    # holder-a's lease expires, holder-b reclaims
    with patch.object(hermes_state.time, "time", lambda: base + 20.0):
        assert db.try_acquire_compression_lock("s1", "holder-b", ttl_seconds=10.0) is True
    # holder-a refresh now must be ownership_lost
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) == "ownership_lost"
    assert db.refresh_compression_lock("s1", "holder-b", ttl_seconds=10.0) == "renewed"


# ---------------------------------------------------------------------------
# 5. old holder cannot extend new holder's lease
# ---------------------------------------------------------------------------
def test_old_holder_cannot_extend_new_lease(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("s1", source="test")
    import hermes_state
    base = 2000.0
    with patch.object(hermes_state.time, "time", lambda: base):
        assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True
        before_b = db._conn.execute("SELECT expires_at FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()[0]
    with patch.object(hermes_state.time, "time", lambda: base + 20.0):
        assert db.try_acquire_compression_lock("s1", "holder-b", ttl_seconds=10.0) is True
        b_expires = db._conn.execute("SELECT expires_at FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()[0]
    # old holder tries to refresh with its TTL — must not touch B's row
    assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=300.0) == "ownership_lost"
    after = db._conn.execute("SELECT expires_at, holder FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()
    assert after[1] == "holder-b"
    assert after[0] == b_expires  # unchanged


# ---------------------------------------------------------------------------
# 6. expired-but-unreclaimed can revive its own lease
# ---------------------------------------------------------------------------
def test_expired_unreclaimed_can_revive(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("s1", source="test")
    import hermes_state
    base = 3000.0
    with patch.object(hermes_state.time, "time", lambda: base):
        assert db.try_acquire_compression_lock("s1", "holder-a", ttl_seconds=10.0) is True
    # Expire it
    with patch.object(hermes_state.time, "time", lambda: base + 20.0):
        # No contender, just expired
        pass
    # holder-a refreshes its own expired row — must succeed (revivable)
    # time is now base+20, row expires_at is base+10, so expired but holder still A
    with patch.object(hermes_state.time, "time", lambda: base + 20.0):
        assert db.refresh_compression_lock("s1", "holder-a", ttl_seconds=10.0) == "renewed"
        new_expires = db._conn.execute("SELECT expires_at FROM compression_locks WHERE session_id=?", ("s1",)).fetchone()[0]
        assert new_expires > base + 20.0


# ---------------------------------------------------------------------------
# 7-8. pre-publication require_lease_refresh interlock with #98867
# ---------------------------------------------------------------------------
def test_publish_with_require_refresh_succeeds_for_legitimate_holder(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("parent-1", source="test")
    _seed_lock(db, "parent-1", "holder-1", expired=True)
    # Legacy path without refresh would fail; with refresh succeeds
    with pytest.raises(CompressionSessionBusyError):
        db.publish_compression_child(
            parent_session_id="parent-1",
            child_session_id="child-fail",
            source="test",
            messages=[{"role": "user", "content": "hi"}],
            compression_lock_holder="holder-1",
            require_compression_lease=True,
            require_lease_refresh=False,
        )
    # Now with refresh
    db.publish_compression_child(
        parent_session_id="parent-1",
        child_session_id="child-ok",
        source="test",
        messages=[{"role": "user", "content": "hi"}],
        compression_lock_holder="holder-1",
        require_compression_lease=True,
        require_lease_refresh=True,
        lease_ttl_seconds=300.0,
    )
    assert db.get_session("child-ok") is not None


def test_publish_refresh_fails_after_reclaim(tmp_path):
    db = _setup_db(tmp_path)
    db.create_session("parent-1", source="test")
    _seed_lock(db, "parent-1", "holder-b", expired=False)
    with pytest.raises(CompressionSessionBusyError, match="lease lost"):
        db.publish_compression_child(
            parent_session_id="parent-1",
            child_session_id="child-1",
            source="test",
            messages=[{"role": "user", "content": "hi"}],
            compression_lock_holder="holder-a",
            require_compression_lease=True,
            require_lease_refresh=True,
        )


# ---------------------------------------------------------------------------
# 9. refresher exits after genuine ownership loss
# ---------------------------------------------------------------------------
def test_refresher_exits_on_ownership_lost():
    from agent.conversation_compression import _CompressionLockLeaseRefresher

    class LostDB:
        calls = 0

        def refresh_compression_lock(self, sid, holder, ttl_seconds=300.0):
            self.calls += 1
            return "ownership_lost"

    db = LostDB()
    refresher = _CompressionLockLeaseRefresher(db, "sess", "holder", ttl_seconds=10.0, refresh_interval_seconds=2.0)
    refresher._stop.wait = lambda _interval: False  # type: ignore
    refresher._run()
    # Should stop immediately on first ownership_lost (not 5)
    assert db.calls == 1


def test_refresher_continues_on_transient():
    """Refresher must survive > cap consecutive transient failures (new contract)."""
    from agent.conversation_compression import _CompressionLockLeaseRefresher

    class TransientDB:
        calls = 0

        def refresh_compression_lock(self, sid, holder, ttl_seconds=300.0):
            self.calls += 1
            if self.calls >= 6:
                # After 5 transients, 6th is renewed — stop after proving survival
                # Access refresher via closure set below
                try:
                    refresher_ref["ref"]._stop.set()
                except Exception:
                    pass
                return "renewed"
            return "transient_failure"

    db = TransientDB()
    refresher = _CompressionLockLeaseRefresher(db, "sess", "holder", ttl_seconds=10.0, refresh_interval_seconds=0.05)
    refresher_ref = {"ref": refresher}
    # Fast interval (0.05) keeps test < 0.5s; _run will iterate via wait
    refresher._run()
    assert db.calls >= 6, "refresher must have survived 5 transients and reached 6th renewed"


# ---------------------------------------------------------------------------
# Interlock with #98867: both require_lease_refresh modes
# ---------------------------------------------------------------------------
def test_98867_interlock_both_modes(tmp_path):
    """Ensure #98867's False-path and our True-path cannot both be green."""
    db = _setup_db(tmp_path)
    db.create_session("parent-98867", source="test")
    _seed_lock(db, "parent-98867", "holder-1", expired=True)
    # With require_lease_refresh=False (as #98867 tests) -> must fail
    with pytest.raises(CompressionSessionBusyError):
        db.publish_compression_child(
            parent_session_id="parent-98867",
            child_session_id="child-a",
            source="test",
            messages=[{"role": "user", "content": "hi"}],
            compression_lock_holder="holder-1",
            require_compression_lease=True,
            require_lease_refresh=False,
        )
    # With require_lease_refresh=True (our prod path) -> must succeed
    db.publish_compression_child(
        parent_session_id="parent-98867",
        child_session_id="child-b",
        source="test",
        messages=[{"role": "user", "content": "hi"}],
        compression_lock_holder="holder-1",
        require_compression_lease=True,
        require_lease_refresh=True,
    )
    assert db.get_session("child-b") is not None
