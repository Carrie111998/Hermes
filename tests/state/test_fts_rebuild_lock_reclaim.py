"""Regression tests for #100108: FTS rebuild lock dead-holder reclamation.

A wedged/zombie holder can keep the flock while no longer doing FTS work,
wedging every future rebuild into the defer path for hours (the reported
incident: stale locks from Aug 30 blocked all rebuilds until a manual
optimize pass on Sep 1). The fix probes whether ANY live process still
holds the DB open when the bounded acquire times out; none does => the
leaked lock is reclaimed and the rebuild proceeds.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_state_common import (
    _FTS_REBUILD_LOCK_POLL_SECONDS,
    _FTS_REBUILD_LOCK_TIMEOUT_SECONDS,
    _lock_holder_is_live,
    fts_rebuild_admission,
)

linux_only = pytest.mark.linux_only


class TestLockHolderIsLive:
    def test_open_file_reports_live(self, tmp_path):
        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        fh = open(db, "rb")
        try:
            assert _lock_holder_is_live(db) is True
        finally:
            fh.close()

    @linux_only
    def test_closed_unheld_file_reports_not_live(self, tmp_path):
        """No process holds the DB open -> the probe must report not-live."""
        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        # No open handle: fuser/procfs must report no holder. On CI hosts
        # where /proc exists this is deterministic; the fuser branch is
        # the same answer.
        assert _lock_holder_is_live(db) is False

    def test_probe_failure_fails_safe_true(self, tmp_path):
        """A raising probe must NEVER authorize breaking a live lock."""
        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        import subprocess as sp

        with patch.object(sp, "run", side_effect=OSError("boom")):
            # OSError is caught by the except in _lock_holder_is_live -> True
            assert _lock_holder_is_live(db) is True

    @linux_only
    def test_missing_db_file_fails_safe_true(self, tmp_path):
        """A DB path that doesn't exist cannot be probed -> True (safe)."""
        db = tmp_path / "never-created.db"
        assert _lock_holder_is_live(db) is True


@linux_only
class TestFtsRebuildAdmissionReclamation:
    def test_dead_holder_lock_is_reclaimed(self, tmp_path):
        """The #100108 shape: flock held, but NO process holds the DB open.

        Simulate the wedged holder: a subprocess takes the flock and exits
        WITHOUT closing... is impossible cleanly (kernel releases at exit).
        Instead: simulate a leaked lock by holding the flock on a handle to
        the LOCK file while no process holds the DB open — exactly the
        incident's observable state (lock file busy, DB unheld).
        """
        import fcntl

        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        lock_path = f"{db}.fts_rebuild.lock"

        # Hold the flock from THIS process (we hold the lock file open, so
        # the lock is "live" as far as flock is concerned) but the DB file
        # itself is NOT held open by anyone.
        with open(lock_path, "a+b") as blocker:
            fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                # Shrink the timeout so the test is fast; poll fast too.
                with patch(
                    "hermes_state_common._FTS_REBUILD_LOCK_TIMEOUT_SECONDS", 0.3
                ), patch(
                    "hermes_state_common._FTS_REBUILD_LOCK_POLL_SECONDS", 0.02
                ):
                    # THIS process holds the lock — a second acquire from the
                    # same process on a NEW fd still blocks (flock is per-fd).
                    # The DB is held open only by... nobody (db was closed).
                    # The probe: is any process holding db open? No. So the
                    # reclamation must fire and the admission must succeed.
                    with fts_rebuild_admission(db) as admitted:
                        assert admitted is True, (
                            "dead-holder lock (flock held, DB unheld) must be "
                            "reclaimed per #100108"
                        )
            finally:
                fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)

    def test_live_db_holder_still_defers(self, tmp_path):
        """A process genuinely holding the DB open must keep the defer.

        Even when the flock is held by another fd, if a LIVE process holds
        the DB open, the reclamation must NOT fire (that holder could be
        mid-rebuild) — the admission defers as before.
        """
        import fcntl

        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        lock_path = f"{db}.fts_rebuild.lock"

        db_handle = open(db, "rb")  # a live process (this one) holds the DB
        with open(lock_path, "a+b") as blocker:
            fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with patch(
                    "hermes_state_common._FTS_REBUILD_LOCK_TIMEOUT_SECONDS", 0.3
                ), patch(
                    "hermes_state_common._FTS_REBUILD_LOCK_POLL_SECONDS", 0.02
                ):
                    with fts_rebuild_admission(db) as admitted:
                        assert admitted is False, (
                            "live DB holder must keep the defer — breaking the "
                            "lock risks concurrent-rebuild corruption"
                        )
            finally:
                fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        db_handle.close()

    def test_uncontended_lock_acquires_normally(self, tmp_path):
        """Baseline: no holder -> immediate acquire, no probe call."""
        db = tmp_path / "state.db"
        db.write_bytes(b"x")
        with patch(
            "hermes_state_common._lock_holder_is_live",
            side_effect=AssertionError("probe must not run on clean acquire"),
        ):
            with fts_rebuild_admission(db) as admitted:
                assert admitted is True

    def test_none_db_yields_true(self):
        with fts_rebuild_admission(None) as admitted:
            assert admitted is True
