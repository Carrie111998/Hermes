from __future__ import annotations

import json
import unittest

from hermes_cli.kanban_store.claims import heartbeat, invalidate_claim, issue_claim, verify_fence
from hermes_cli.kanban_store.schema import migrate, schema_digest
from hermes_cli.kanban_store.types import FenceConflict, RunFence

from .helpers import TempRoot, add_task, database


class SchemaClaimTests(unittest.TestCase):
    def test_migration_is_idempotent_and_orders_legacy_events(self):
        with TempRoot() as root:
            conn = database(root)
            conn.execute(
                "INSERT INTO task_events(event_uuid,task_id,schema_version,event_type,source,severity,retention_class,host_committed_at,payload_json) VALUES('e', 't', 1, 'x','s','info','audit',1,'{}')"
            )
            before = schema_digest(conn)
            first = migrate(conn)
            second = migrate(conn)
            self.assertEqual(first["board_id"], second["board_id"])
            self.assertEqual(before, schema_digest(conn))
            self.assertEqual(conn.execute("SELECT event_seq FROM task_events").fetchone()[0], 1)

    def test_claim_rotates_generation_and_returns_plaintext_once(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            row = conn.execute("SELECT claim_token_hash,claim_generation FROM tasks").fetchone()
            self.assertNotEqual(row["claim_token_hash"], claim.fence.claim_token)
            self.assertEqual(row["claim_generation"], 1)
            verify_fence(conn, claim.fence)

    def test_wrong_token_is_fenced(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            wrong = RunFence("task-1", claim.fence.run_id, 1, "x" * 64)
            with self.assertRaises(FenceConflict):
                verify_fence(conn, wrong)

    def test_heartbeat_is_liveness_only(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            heartbeat(conn, claim.fence, ttl_seconds=60, now=100)
            event = conn.execute("SELECT event_type,payload_json FROM task_events ORDER BY event_seq DESC LIMIT 1").fetchone()
            self.assertEqual(event[0], "run.heartbeat")
            self.assertNotIn("motion", json.loads(event[1]))

    def test_invalidation_advances_generation_before_requeue(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            next_generation = invalidate_claim(
                conn,
                task_id="task-1",
                run_id=claim.fence.run_id,
                claim_generation=claim.fence.claim_generation,
                reason="reclaimed",
            )
            self.assertEqual(next_generation, 2)
            row = conn.execute("SELECT status,claim_generation,claim_token_hash FROM tasks").fetchone()
            self.assertEqual(tuple(row), ("ready", 2, None))
            with self.assertRaises(FenceConflict):
                verify_fence(conn, claim.fence)


if __name__ == "__main__":
    unittest.main()
