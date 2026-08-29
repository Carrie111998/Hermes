"""skill_manage operations[] batch (#95681 arc, maintainer-approved).

Memory-tool pattern: several ops on ONE skill, atomically — create + N
supporting files, or SKILL.md + the script it references, in one call.
Any failure rolls the skill directory back to its pre-batch state.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SK = (
    "---\nname: {n}\ndescription: Use when probing batch ops. Behavior.\n---\n"
    "# Probe\nStep 1.\n"
)


class TestSkillManageBatch(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="skmbatch_t_")
        os.environ["HERMES_HOME"] = self.home
        os.environ["HERMES_YOLO_MODE"] = "1"
        os.makedirs(os.path.join(self.home, "skills"), exist_ok=True)
        # Re-import against the temp home (module caches SKILLS_DIR).
        import importlib

        import tools.skill_manager_tool as smt
        importlib.reload(smt)
        self.smt = smt

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _call(self, name, ops):
        # Inject the per-op name (tests were written per-skill; the
        # interface is name-per-op, maintainer-directed).
        for op in ops:
            op.setdefault("name", name)
        return json.loads(self.smt.skill_manage(action="", name="", operations=ops))

    def test_create_plus_files_atomic(self):
        r = self._call("probe", [
            {"action": "create", "content": SK.format(n="probe")},
            {"action": "write_file", "file_path": "references/a.md", "file_content": "a"},
            {"action": "write_file", "file_path": "scripts/r.py", "file_content": "pass"},
        ])
        self.assertTrue(r["success"], r)
        self.assertEqual(r["operations_applied"], 3)
        base = os.path.join(self.home, "skills", "probe")
        for rel in ("SKILL.md", "references/a.md", "scripts/r.py"):
            self.assertTrue(os.path.exists(os.path.join(base, rel)), rel)

    def test_midbatch_failure_rolls_back_existing_skill(self):
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "Step ONE."},
            {"action": "write_file", "file_path": "bad/nope.md", "file_content": "x"},
        ])
        self.assertFalse(r["success"])
        self.assertEqual(r["failed_index"], 1)
        content = open(os.path.join(self.home, "skills", "probe", "SKILL.md")).read()
        self.assertIn("Step 1.", content)       # patch undone
        self.assertNotIn("Step ONE.", content)

    def test_failed_create_batch_removes_partial_skill(self):
        r = self._call("fresh", [
            {"action": "create", "content": SK.format(n="fresh")},
            {"action": "write_file", "file_path": "../escape.md", "file_content": "x"},
        ])
        self.assertFalse(r["success"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "fresh")))

    def test_failed_rollback_preserves_snapshot_for_recovery(self):
        """(#97714) A restore that raises must not destroy both copies.

        The old _rollback() removed the live skill directory BEFORE copying
        the snapshot back; if the copy then raised (disk full, locked file,
        Windows path limits), the finally deleted the snapshot too — the
        skill and its only backup were both gone. A failed restore must
        leave the operator a recoverable copy, and the error payload must
        say so instead of implying the rollback ran.
        """
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        smt = self.smt
        real_copytree = shutil.copytree
        skills_dir = os.path.join(self.home, "skills")
        state = {"armed": False, "snap_dir": None}

        real_rmtree = shutil.rmtree
        surviving_snaps = []

        def tracking_rmtree(path, *a, **k):
            p = os.fspath(path).replace("\\", "/")
            # record every snapshot dir that gets deleted, so the test can
            # assert whether the backup survived
            if "skill_batch_" in p:
                surviving_snaps.append(p)
            return real_rmtree(path, *a, **k)

        def copytree_gated(src, dst, *a, **k):
            dstp = os.fspath(dst).replace("\\", "/")
            if dstp.startswith(skills_dir.replace("\\", "/")) and state["armed"]:
                raise OSError("simulated restore failure (disk full)")
            return real_copytree(src, dst, *a, **k)

        real_skill_manage = smt.skill_manage

        # The batch loop calls the module-global skill_manage per op; wrap it
        # so the first failing op arms the restore failure (the rollback's
        # copytree then raises on its way back into the skills dir).
        def gated_skill_manage(*a, **k):
            result_raw = real_skill_manage(*a, **k)
            try:
                ok = json.loads(result_raw).get("success")
            except Exception:
                ok = False
            if not ok:
                # this was the failing op — rollback runs next
                state["armed"] = True
            return result_raw

        smt.shutil.copytree = copytree_gated
        smt.shutil.rmtree = tracking_rmtree
        smt.skill_manage = gated_skill_manage
        try:
            r = self._call("probe", [
                {"action": "patch", "old_string": "Step 1.", "new_string": "Step ONE."},
                {"action": "write_file", "file_path": "bad/nope.md", "file_content": "x"},
            ])
        finally:
            smt.shutil.copytree = real_copytree
            smt.shutil.rmtree = real_rmtree
            smt.skill_manage = real_skill_manage

        self.assertFalse(r["success"])
        self.assertIn("ROLLBACK FAILED", r["error"])
        # The recoverable copy must still exist: a snapshot dir that was
        # never deleted. surviving_snaps lists snapshot dirs rmtree touched.
        # With the fix, the finally skips cleanup when rollback failed, so
        # check: at least one skill_batch_* dir under temp remains alive.
        import glob, tempfile as _tf
        leftovers = [
            d for d in glob.glob(os.path.join(_tf.gettempdir(), "skill_batch_*"))
            if os.path.isdir(d) and os.path.exists(
                os.path.join(d, "probe", "SKILL.md"))
        ]
        self.assertTrue(
            leftovers,
            "snapshot was deleted despite failed rollback — no recoverable copy left",
        )
        # and the payload must point the operator at it
        self.assertTrue(
            any("snapshot" in r["error"].lower() for _ in [0]),
            "error payload should tell the operator a snapshot survives",
        )

    def test_validation_rules(self):
        # delete as SOLE op routes to the real delete (works)
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [{"action": "delete"}])
        self.assertTrue(r["success"], r)
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "probe")))
        # delete mixed with other ops rejected
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "X."},
            {"action": "delete"},
        ])
        self.assertFalse(r["success"])
        self.assertIn("SOLE", r["error"])
        # create must be first
        r = self._call("x", [
            {"action": "write_file", "file_path": "references/a.md", "file_content": "a"},
            {"action": "create", "content": SK.format(n="x")},
        ])
        self.assertFalse(r["success"])
        # empty / capped
        r = self._call("x", [])
        self.assertFalse(r["success"])
        r = self._call("x", [{"action": "patch"}] * 21)
        self.assertFalse(r["success"])
        self.assertIn("capped", r["error"])

    def test_intra_batch_conflict_guard(self):
        """Same-file double writes and post-edit full rewrites are always
        a confused plan under last-wins sequencing — rejected BEFORE any
        side effect. Patch chains and rewrite-first stay legal."""
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        # destructive op on an already-touched file: rejected — double
        # write, write+remove, patch-then-write, patch-then-remove, and a
        # path-spelling variant of the same file.
        self._call("probe", [{"action": "write_file",
                              "file_path": "references/c.md", "file_content": "seed"}])
        for ops in (
            [{"action": "write_file", "file_path": "references/a.md", "file_content": "1"},
             {"action": "write_file", "file_path": "references/a.md", "file_content": "2"}],
            [{"action": "write_file", "file_path": "references/b.md", "file_content": "x"},
             {"action": "remove_file", "file_path": "references/b.md"}],
            [{"action": "patch", "file_path": "references/c.md",
              "old_string": "seed", "new_string": "edited"},
             {"action": "write_file", "file_path": "references/c.md", "file_content": "CLOB"}],
            [{"action": "patch", "file_path": "references/c.md",
              "old_string": "seed", "new_string": "edited"},
             {"action": "remove_file", "file_path": "references/c.md"}],
            [{"action": "write_file", "file_path": "references/d.md", "file_content": "1"},
             {"action": "write_file", "file_path": "./references//d.md", "file_content": "2"}],
        ):
            r = self._call("probe", ops)
            self.assertFalse(r["success"], ops)
            self.assertIn("discard", r["error"])
        # ...and rejected pre-effect: c.md still holds its seed text.
        c_md = os.path.join(self.home, "skills", "probe", "references", "c.md")
        self.assertEqual(open(c_md).read(), "seed")
        # write-then-patch on one supporting file stays legal (additive).
        r = self._call("probe", [
            {"action": "write_file", "file_path": "references/e.md", "file_content": "base"},
            {"action": "patch", "file_path": "references/e.md",
             "old_string": "base", "new_string": "base+"},
        ])
        self.assertTrue(r["success"], r)
        # patch then full rewrite: rejected; rewrite-first: allowed
        r = self._call("probe", [
            {"action": "patch", "old_string": "Step 1.", "new_string": "P."},
            {"action": "patch", "content": SK.format(n="probe")},
        ])
        self.assertFalse(r["success"])
        self.assertIn("rewrite", r["error"])
        r = self._call("probe", [
            {"action": "patch", "content": SK.format(n="probe").replace("Step 1.", "F.")},
            {"action": "patch", "old_string": "F.", "new_string": "G."},
        ])
        self.assertTrue(r["success"], r)
        # patch chains stay legal
        r = self._call("probe", [
            {"action": "patch", "old_string": "G.", "new_string": "H."},
            {"action": "patch", "old_string": "H.", "new_string": "I."},
        ])
        self.assertTrue(r["success"], r)

    def test_cross_skill_batch_and_rollback(self):
        """Ops may target DIFFERENT skills; a late failure rolls back
        every touched skill, including removing a batch-created one."""
        self._call("alpha", [{"action": "create", "content": SK.format(n="alpha")}])
        r = json.loads(self.smt.skill_manage(action="", name="", operations=[
            {"name": "alpha", "action": "patch",
             "old_string": "Step 1.", "new_string": "Step A."},
            {"name": "beta", "action": "create", "content": SK.format(n="beta")},
            {"name": "beta", "action": "write_file",
             "file_path": "bad/nope.md", "file_content": "x"},
        ]))
        self.assertFalse(r["success"])
        self.assertEqual(r["failed_index"], 2)
        # alpha's patch undone; beta (batch-created) removed entirely.
        content = open(os.path.join(self.home, "skills", "alpha", "SKILL.md")).read()
        self.assertIn("Step 1.", content)
        self.assertNotIn("Step A.", content)
        self.assertFalse(os.path.exists(os.path.join(self.home, "skills", "beta")))

    def test_single_op_path_unchanged(self):
        self._call("probe", [{"action": "create", "content": SK.format(n="probe")}])
        raw = self.smt.skill_manage(
            action="patch", name="probe",
            old_string="Step 1.", new_string="Step 1 (single).",
        )
        self.assertTrue(json.loads(raw)["success"])

    def test_batch_stages_as_one_pending_write_when_gated(self):
        """Approval gate: the whole batch stages as ONE pending record, and
        apply_skill_pending replays it (operations key round-trips)."""
        from unittest.mock import patch as _patch

        class _Decision:
            allow = False
            blocked = False
            message = "staged for review"

        staged = {}

        def fake_stage_write(area, payload, summary=None, origin=None):
            staged.update(payload=payload, summary=summary)
            return {"id": "pend_1"}

        import tools.write_approval as wa

        with _patch.object(wa, "evaluate_gate", return_value=_Decision()), \
             _patch.object(wa, "stage_write", side_effect=fake_stage_write):
            r = self._call("probe", [
                {"action": "create", "content": SK.format(n="probe")},
                {"action": "write_file", "file_path": "references/a.md",
                 "file_content": "a"},
            ])
        self.assertTrue(r.get("staged"), r)
        self.assertEqual(staged["payload"]["action"], "batch")
        self.assertEqual(len(staged["payload"]["operations"]), 2)
        self.assertIn("2 ops", staged["summary"])
        # Replay applies the batch (gate bypassed inside).
        out = json.loads(self.smt.apply_skill_pending(staged["payload"]))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["operations_applied"], 2)


if __name__ == "__main__":
    unittest.main()
