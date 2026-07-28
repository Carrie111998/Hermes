"""Tests for SessionDB WAL checkpoint strategy (issue #45383) and cross-process
checkpoint contention guard (issue #73411).

Verifies that periodic checkpoints use PASSIVE mode (safe for large DBs),
that a cross-process flock gate prevents concurrent checkpoint operations
from separate SessionDB instances, and that close() / pre-VACUUM paths still
use TRUNCATE.
"""

import sqlite3
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


class TestTryWalCheckpointPassive:
    """_try_wal_checkpoint() should use PASSIVE mode for periodic use."""

    def test_checkpoint_uses_passive_mode(self, db):
        """PASSIVE checkpoint does not require exclusive lock — safe for large DBs."""
        # Capture the real connection's execute before mocking
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        # sqlite3.Connection.execute is read-only (C extension) — replace _conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        mock_conn.fetchone.return_value = None
        db._conn = mock_conn

        db._try_wal_checkpoint()

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint call, got {len(passive_calls)}"
        )
        assert len(truncate_calls) == 0, (
            "Periodic checkpoint should NOT use TRUNCATE"
        )

    def test_checkpoint_logs_warning_on_failure(self, db, caplog):
        """Failed PASSIVE checkpoint logs a warning instead of silent pass."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        db._conn = mock_conn
        # Bypass the flock gate so we exercise the actual PRAGMA failure path.
        fh = db._try_checkpoint_flock(db.db_path)
        assert fh is not None, "flock gate must succeed in single-process test"
        fh.close()
        with patch.object(db, "_try_checkpoint_flock", return_value=fh):
            with caplog.at_level(logging.WARNING):
                db._try_wal_checkpoint()

        assert any("WAL checkpoint (PASSIVE) failed" in r.message for r in caplog.records), (
            f"Expected warning log about PASSIVE checkpoint failure, got: {caplog.text}"
        )

    def test_checkpoint_returns_result_on_success(self, db):
        """Successful PASSIVE checkpoint does not raise."""
        db._try_wal_checkpoint()

    def test_checkpoint_skipped_when_flock_unavailable(self, db, caplog):
        """When another process holds the checkpoint flock, checkpoint is skipped
        silently (no warning) and no PRAGMA is issued."""
        mock_conn = MagicMock()
        db._conn = mock_conn
        # Simulate another process holding the lock.
        with patch.object(db, "_try_checkpoint_flock", return_value=None):
            with caplog.at_level(logging.WARNING):
                db._try_wal_checkpoint()
            # No PRAGMA call, no warning.
            mock_conn.execute.assert_not_called()
            assert not any("WAL checkpoint" in r.message for r in caplog.records)


class TestCloseUsesTruncate:
    """close() should still use TRUNCATE to shrink WAL on shutdown."""

    def test_close_uses_truncate_mode(self, db):
        """TRUNCATE at close is safe — no concurrent writers during shutdown."""
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        db.close()

        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(truncate_calls) == 1, (
            f"Expected 1 TRUNCATE checkpoint at close, got {len(truncate_calls)}"
        )

    def test_close_logs_debug_on_failure(self, db, caplog):
        """Failed TRUNCATE at close logs debug (not warning — close is best-effort)."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        db._conn = mock_conn

        with caplog.at_level(logging.DEBUG):
            db.close()

        assert any("WAL checkpoint (TRUNCATE) at close failed" in r.message for r in caplog.records), (
            f"Expected debug log about TRUNCATE failure at close, got: {caplog.text}"
        )


class TestCheckpointFrequency:
    """Checkpoint triggers every N writes."""

    def test_checkpoint_triggers_at_interval(self, db):
        """_try_wal_checkpoint is called every _CHECKPOINT_EVERY_N_WRITES writes."""
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        # Write exactly _CHECKPOINT_EVERY_N_WRITES sessions to trigger one checkpoint
        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        assert call_count[0] == 1, (
            f"Expected 1 checkpoint after {n} writes, got {call_count[0]}"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
class TestCrossProcessCheckpointFlock:
    """Real cross-process flock gate for WAL checkpoints (issue #73411)."""

    def test_checkpoint_flock_allows_single_process(self, tmp_path):
        """A lone process can acquire the checkpoint flock."""
        db_path = tmp_path / "test.db"
        fh = SessionDB._try_checkpoint_flock(db_path)
        assert fh is not None
        fh.close()

    def test_checkpoint_flock_blocks_second_holder(self, tmp_path):
        """When one fd holds the checkpoint flock, a second open() gets None."""
        fcntl = pytest.importorskip("fcntl")
        db_path = tmp_path / "test.db"
        # Hold the lock on a raw fd (simulates another process).
        lock_path = db_path.with_suffix(db_path.suffix + ".checkpoint.lock")
        blocker_fh = open(lock_path, "a", encoding="utf-8")
        fcntl.flock(blocker_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Same-process second fd contends because flock is per-open-fd.
            result = SessionDB._try_checkpoint_flock(db_path)
            assert result is None, (
                "Expected None when checkpoint flock is already held"
            )
        finally:
            fcntl.flock(blocker_fh.fileno(), fcntl.LOCK_UN)
            blocker_fh.close()

    def test_checkpoint_flock_released_after_close(self, tmp_path):
        """Releasing the flock allows the next holder to acquire it."""
        db_path = tmp_path / "test.db"
        fh1 = SessionDB._try_checkpoint_flock(db_path)
        assert fh1 is not None
        fh1.close()
        fh2 = SessionDB._try_checkpoint_flock(db_path)
        assert fh2 is not None, (
            "Flock should be acquirable after the first holder closes"
        )
        fh2.close()

    def test_checkpoint_flock_releases_lock_on_close(self, tmp_path):
        """Closing the flock fd releases the exclusive lock."""
        fcntl = pytest.importorskip("fcntl")
        db_path = tmp_path / "test.db"
        fh = SessionDB._try_checkpoint_flock(db_path)
        assert fh is not None
        fh.close()
        # After close, another fd should be able to acquire immediately.
        fh2 = open(
            db_path.with_suffix(db_path.suffix + ".checkpoint.lock"),
            "a",
            encoding="utf-8",
        )
        try:
            fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fh2.fileno(), fcntl.LOCK_UN)
            fh2.close()
