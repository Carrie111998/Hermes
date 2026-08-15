from __future__ import annotations

import unittest

from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.finalization import finalize_worker_run
from hermes_cli.kanban_store.publication import (
    approve_intent,
    claim_dispatch,
    mark_dispatch_started,
    record_dispatch_outcome,
    load_dispatch_contract,
)
from hermes_cli.kanban_publisher.controller import _contract
from hermes_cli.kanban_store.reconciliation import (
    ReconciliationResult,
    begin_reconciliation,
    finish_reconciliation,
)
from hermes_cli.kanban_store.types import (
    ContractError,
    DispatchDisposition,
    DispatchOutcome,
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


def sealed(conn, root, outcome="completed"):
    add_task(conn)
    claim = issue_claim(conn, task_id="task-1", profile="worker", ttl_seconds=60)
    result = finalize_worker_run(
        conn,
        request=FinalizationRequest(
            claim.fence,
            outcome,
            "summary",
            draft_intents=(
                DraftIntent(
                    PublicationKind.GITHUB_ISSUE_CREATE,
                    {},
                    {"title": "Title", "body": "Body"},
                    "nonce",
                ),
            ),
        ),
        workspace=root,
        artifact_blob_root=root / "blobs",
        policy_resolver=policy,
    )
    return result["intents"][0]


class PublicationTests(unittest.TestCase):
    def test_approval_is_exact_digest_bound(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            with self.assertRaises(ContractError):
                approve_intent(
                    conn,
                    intent_id=intent["intent_id"],
                    wire_sha256="0" * 64,
                    actor="operator",
                    decision="approve",
                )

    def test_one_dispatch_per_approval(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(conn, approval_id=approval, controller_id="publisher-1")
            self.assertTrue(dispatch)
            with self.assertRaises(ContractError):
                claim_dispatch(conn, approval_id=approval, controller_id="publisher-2")


    def test_projection_drift_is_rejected_before_dispatch(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(
                conn, approval_id=approval, controller_id="publisher"
            )
            conn.execute(
                "UPDATE publication_intents SET payload_json=? WHERE intent_id=?",
                ('{"body":"tampered"}', intent["intent_id"]),
            )
            with self.assertRaises(ContractError):
                _contract(load_dispatch_contract(conn, dispatch))

    def test_success_settles_task_after_dispatch_started_fact(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(conn, approval_id=approval, controller_id="publisher")
            mark_dispatch_started(conn, dispatch)
            record_dispatch_outcome(
                conn,
                dispatch,
                DispatchOutcome(DispatchDisposition.SUCCESS, remote_identity="NODE"),
            )
            task = conn.execute("SELECT status,publication_state FROM tasks").fetchone()
            self.assertEqual(tuple(task), ("done", "settled"))

    def test_ambiguous_never_redispatches_and_requires_reconciliation(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(conn, approval_id=approval, controller_id="publisher")
            mark_dispatch_started(conn, dispatch)
            record_dispatch_outcome(
                conn,
                dispatch,
                DispatchOutcome(DispatchDisposition.AMBIGUOUS, detail_code="timeout"),
            )
            self.assertEqual(
                conn.execute("SELECT state FROM publication_intents").fetchone()[0],
                "reconcile_required",
            )
            with self.assertRaises(ContractError):
                claim_dispatch(conn, approval_id=approval, controller_id="publisher-2")

    def test_complete_no_match_remains_attention_not_resend(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root)
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(conn, approval_id=approval, controller_id="publisher")
            mark_dispatch_started(conn, dispatch)
            record_dispatch_outcome(
                conn,
                dispatch,
                DispatchOutcome(DispatchDisposition.AMBIGUOUS, detail_code="timeout"),
            )
            reconciliation = begin_reconciliation(
                conn, intent_id=intent["intent_id"], actor="operator"
            )
            outcome = finish_reconciliation(
                conn,
                reconciliation_id=reconciliation,
                result=ReconciliationResult(True, (), "complete_pagination", {}),
            )
            self.assertEqual(outcome, "no_match")
            self.assertEqual(
                conn.execute("SELECT state FROM publication_intents").fetchone()[0],
                "reconcile_required",
            )

    def test_blocked_outcome_returns_to_blocked_after_required_publication(self):
        with TempRoot() as root:
            conn = database(root)
            intent = sealed(conn, root, outcome="blocked")
            approval = approve_intent(
                conn,
                intent_id=intent["intent_id"],
                wire_sha256=intent["wire_sha256"],
                actor="operator",
                decision="approve",
            )
            dispatch = claim_dispatch(conn, approval_id=approval, controller_id="publisher")
            mark_dispatch_started(conn, dispatch)
            record_dispatch_outcome(
                conn,
                dispatch,
                DispatchOutcome(DispatchDisposition.SUCCESS, remote_identity="NODE"),
            )
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "blocked")


if __name__ == "__main__":
    unittest.main()
