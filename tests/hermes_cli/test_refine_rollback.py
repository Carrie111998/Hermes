"""Hermetic unit tests for the ``/refine`` rollback harness.

These exercise the pure snapshot/restore logic with only stdlib + pathlib,
so they run without the agent runtime. Validation step: ``python3 -m
unittest tests/hermes_cli/test_refine_rollback.py`` from the worktree root,
or ``pytest tests/hermes_cli/test_review_rollback.py`` with the runtime venv.

This is the F1 acceptance gate: the snapshot-before-write + atomic restore
contract must hold, INCLUDING the data-loss guard, before any integration
wiring lands.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.refine_rollback import (
    delete_snapshot,
    latest_snapshot_id,
    list_snapshots,
    restore_snapshot,
    take_snapshot,
)


class ReviewRollbackTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rbtest_"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.session = "sess-123"
        self.mem = self.home / "memories"
        self.skills = self.home / "skills"
        self.mem.mkdir()
        self.skills.mkdir()
        (self.mem / "a.txt").write_text("original-a")
        (self.mem / "sub").mkdir()
        (self.mem / "sub" / "b.txt").write_text("original-b")
        (self.skills / "s.txt").write_text("original-skill")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_take_snapshot_copies_all_files(self):
        sid = take_snapshot(self.home, self.session, [self.mem, self.skills])
        self.assertTrue((self.home / "review_snapshots" / sid).exists())
        snap_mem = self.home / "review_snapshots" / sid / "memories"
        self.assertTrue((snap_mem / "a.txt").exists())
        self.assertTrue((snap_mem / "sub" / "b.txt").exists())
        self.assertTrue(
            (self.home / "review_snapshots" / sid / "skills" / "s.txt").exists()
        )

    def test_latest_snapshot_indexed_by_session(self):
        sid = take_snapshot(self.home, self.session, [self.mem])
        self.assertEqual(latest_snapshot_id(self.home, self.session), sid)

    def test_restore_reverts_modifications(self):
        sid = take_snapshot(self.home, self.session, [self.mem, self.skills])
        (self.mem / "a.txt").write_text("CHANGED")
        (self.mem / "new.txt").write_text("fork-added")
        (self.skills / "s.txt").write_text("CHANGED")
        res = restore_snapshot(self.home, sid)
        self.assertEqual(res, {"applied": True, "skipped": []})
        self.assertEqual((self.mem / "a.txt").read_text(), "original-a")
        self.assertEqual((self.skills / "s.txt").read_text(), "original-skill")
        self.assertFalse((self.mem / "new.txt").exists())

    def test_restore_missing_returns_not_applied(self):
        res = restore_snapshot(self.home, "does-not-exist")
        self.assertEqual(res, {"applied": False, "skipped": []})

    def test_list_snapshots_filters_by_session(self):
        take_snapshot(self.home, self.session, [self.mem])
        take_snapshot(self.home, "other-session", [self.mem])
        ids = list_snapshots(self.home, self.session)
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith(f"{self.session}_"))

    def test_delete_snapshot_removes_tree(self):
        sid = take_snapshot(self.home, self.session, [self.mem])
        self.assertTrue(delete_snapshot(self.home, sid))
        self.assertFalse((self.home / "review_snapshots" / sid).exists())
        self.assertFalse(delete_snapshot(self.home, sid))  # already gone

    def test_corruption_guard_never_wipes_live_dir(self):
        """A snapshot claiming files but storing none must NOT empty live."""
        sid = take_snapshot(self.home, self.session, [self.mem, self.skills])
        snapdir = self.home / "review_snapshots" / sid
        # Corrupt: manifest promises 5 files for memories, but storage is empty.
        mani = json.loads((snapdir / "manifest.json").read_text())
        mani["dirs"][0]["files"] = 5
        (snapdir / "manifest.json").write_text(json.dumps(mani))
        for f in (snapdir / "memories").rglob("*"):
            if f.is_file():
                f.unlink()
        # Fork writes important data after snapshot.
        (self.mem / "a.txt").write_text("IMPORTANT-USER-DATA")
        res = restore_snapshot(self.home, sid)
        self.assertTrue(res["applied"])
        self.assertIn(str(self.mem), res["skipped"])
        # Live data must be preserved (no silent wipe).
        self.assertEqual((self.mem / "a.txt").read_text(), "IMPORTANT-USER-DATA")

    def test_empty_target_undo_clears_fork_added_files(self):
        """Legitimate empty-target snapshot restores to empty (undo works)."""
        empty = self.home / "scratch"
        empty.mkdir()
        sid = take_snapshot(self.home, self.session, [empty])
        (empty / "fork.txt").write_text("x")
        res = restore_snapshot(self.home, sid)
        self.assertEqual(res, {"applied": True, "skipped": []})
        self.assertFalse(any(p.is_file() for p in empty.rglob("*")))


if __name__ == "__main__":
    unittest.main()
