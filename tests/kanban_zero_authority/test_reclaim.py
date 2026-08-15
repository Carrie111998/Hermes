from __future__ import annotations

import unittest

from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.reclaim import reclaim_if_safe

from .helpers import TempRoot, add_task, database


def observation(identity, *, process="dead", motion=False):
    return {
        "observation_id": identity,
        "fresh_until": 9999999999,
        "complete": True,
        "coverage": "strong",
        "process": process,
        "worker_motion": motion,
        "idle_window_complete": True,
        "artifacts": "absent",
        "publication": "absent",
        "freeze_supported": True,
        "generation_match": True,
    }


class ReclaimTests(unittest.TestCase):
    def test_dead_scope_reclaim_advances_generation(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            terminated = []
            ok = reclaim_if_safe(
                conn,
                fence=claim.fence,
                initial_observations=[observation("a"), observation("b")],
                resample=lambda _f: [observation("c"), observation("d")],
                freeze_scope=lambda _f: "unused",
                terminate_scope=lambda _f, token: terminated.append(token),
                thaw_scope=lambda _f, _t: None,
            )
            self.assertTrue(ok)
            self.assertEqual(terminated, ["dead"])
            self.assertEqual(tuple(conn.execute("SELECT claim_generation,status FROM tasks").fetchone()), (2, "ready"))

    def test_inert_scope_freezes_then_terminates(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            actions = []
            ok = reclaim_if_safe(
                conn,
                fence=claim.fence,
                initial_observations=[observation("a", process="alive"), observation("b", process="alive")],
                resample=lambda _f: [observation("c", process="alive"), observation("d", process="alive")],
                freeze_scope=lambda _f: actions.append("freeze") or "token",
                terminate_scope=lambda _f, token: actions.append("terminate:" + token),
                thaw_scope=lambda _f, _t: actions.append("thaw"),
            )
            self.assertTrue(ok)
            self.assertEqual(actions, ["freeze", "terminate:token"])

    def test_changed_final_probe_aborts_and_thaws(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            actions = []
            ok = reclaim_if_safe(
                conn,
                fence=claim.fence,
                initial_observations=[observation("a", process="alive"), observation("b", process="alive")],
                resample=lambda _f: [observation("c", process="alive", motion=True), observation("d", process="alive")],
                freeze_scope=lambda _f: actions.append("freeze") or "token",
                terminate_scope=lambda _f, token: actions.append("terminate:" + token),
                thaw_scope=lambda _f, _t: actions.append("thaw"),
            )
            self.assertFalse(ok)
            self.assertEqual(actions, ["freeze", "thaw"])
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "running")

    def test_unknown_or_present_evidence_never_reclaims(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            item = observation("a")
            item["artifacts"] = "present"
            self.assertFalse(
                reclaim_if_safe(
                    conn,
                    fence=claim.fence,
                    initial_observations=[item],
                    resample=lambda _f: [],
                    freeze_scope=lambda _f: "x",
                    terminate_scope=lambda _f, _t: None,
                    thaw_scope=lambda _f, _t: None,
                )
            )


if __name__ == "__main__":
    unittest.main()
