from __future__ import annotations

import sqlite3
import unittest

from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.finalization import finalize_worker_run
from hermes_cli.kanban_store.types import (
    AlreadyFinalized,
    ArtifactDeclaration,
    DraftIntent,
    FinalizationRequest,
    PublicationKind,
    TrustedIntentPolicy,
)

from .helpers import TempRoot, add_task, database


def policy(_draft):
    return TrustedIntentPolicy(
        True,
        "github-app:1",
        "github-issues-v1",
        {"repository_id": 1, "owner": "NousResearch", "repo": "hermes-agent"},
    )


class FinalizationTests(unittest.TestCase):
    def test_atomic_finalization_freezes_artifact_and_seals_intent(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            (root / "out.txt").write_text("hello")
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            request = FinalizationRequest(
                claim.fence,
                "completed",
                "done",
                artifacts=(ArtifactDeclaration("out.txt", "out", "text/plain"),),
                draft_intents=(
                    DraftIntent(
                        PublicationKind.GITHUB_ISSUE_CREATE,
                        {},
                        {"title": "Result", "body": "Body"},
                        "nonce",
                    ),
                ),
            )
            result = finalize_worker_run(
                conn,
                request=request,
                workspace=root,
                artifact_blob_root=root / "blobs",
                policy_resolver=policy,
            )
            self.assertFalse(result["published"])
            self.assertEqual(result["state"], "awaiting_publication")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM finalizations").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM publication_intents").fetchone()[0], 1)

    def test_finalization_is_once_per_generation(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            request = FinalizationRequest(claim.fence, "completed", "done")
            finalize_worker_run(
                conn, request=request, workspace=root, artifact_blob_root=root / "blobs", policy_resolver=policy
            )
            with self.assertRaises(Exception):
                finalize_worker_run(
                    conn, request=request, workspace=root, artifact_blob_root=root / "blobs", policy_resolver=policy
                )

    def test_failpoint_rolls_back_all_database_visibility(self):
        with TempRoot() as root:
            conn = database(root)
            add_task(conn)
            claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
            request = FinalizationRequest(claim.fence, "completed", "done")
            def fail(_name, statement):
                if statement == 2:
                    raise RuntimeError("crash")
            with self.assertRaises(RuntimeError):
                finalize_worker_run(
                    conn,
                    request=request,
                    workspace=root,
                    artifact_blob_root=root / "blobs",
                    policy_resolver=policy,
                    failpoint=fail,
                )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM finalizations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "running")


if __name__ == "__main__":
    unittest.main()
