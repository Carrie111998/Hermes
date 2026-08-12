"""Synthetic, no-network tests for the human-review outbox contract."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway import kanban_human_review as delivery
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_human_review as hr


BOARD = "echlon-linear-fixes"
REPO = "Echlon-Bank/Echlon-Bank"
HEAD = "d" * 40


def _packet(implementation_id: str):
    return {
        "schema_version": 1,
        "board": BOARD,
        "gate_kind": "srdja_pr_review",
        "reviewer_principal": "github:p-echlon",
        "notification_principal": "slack:U0AA6S8RX5M",
        "human_assignee": "srdja",
        "linear_issue_id": "ECH-888",
        "linear_title": "Synthetic human-review gate",
        "linear_issue_url": "https://linear.app/echlon/issue/ECH-888/test",
        "repo": REPO,
        "pr_number": 88,
        "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/88",
        "base_branch": "main",
        "head_branch": "ech-888-human-gate",
        "approved_head_sha": HEAD,
        "implementation_task_id": implementation_id,
        "qa_verdict": "APPROVE_FOR_SRDJA_REVIEW",
        "qa_attempt_count": 0,
        "coder_correction_attempt_count": 0,
        "changed_files": ["service.py"],
        "claimed_fix_summary": "Adds an exact-head human-review gate.",
        "tests_or_checks_run": [{"command": "pytest", "outcome": "passed"}],
        "verification_output": ["passed"],
        "regression_checks": ["review semantics unchanged"],
        "blockers": [],
        "known_risks": [],
        "unchecked_items": ["live delivery disabled"],
        "external_side_effects": "none",
        "requires_srdja_review": True,
        "merge_policy": "human_only",
        "coderabbit": {
            "status": "no_actionable_comments",
            "disposition": "",
            "actionable_count": 0,
            "unresolved_count": 0,
        },
    }


def _snapshot(**overrides):
    snapshot = {
        "source": "github_readback",
        "verified_at": int(time.time()),
        "repo": REPO,
        "pr_number": 88,
        "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/88",
        "state": "OPEN",
        "is_draft": False,
        "base_branch": "main",
        "head_branch": "ech-888-human-gate",
        "head_sha": HEAD,
    }
    snapshot.update(overrides)
    return snapshot


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    db_path = home / "kanban" / "boards" / BOARD / "kanban.db"
    kb.init_db(db_path)
    with kb.connect(db_path) as conn:
        implementation_id = kb.create_task(
            conn,
            title="implementation",
            assignee="echlon-coder",
            board=BOARD,
        )
        assert kb.claim_task(conn, implementation_id, claimer="coder") is not None
        assert kb.complete_task(
            conn,
            implementation_id,
            summary="done",
            metadata={
                "linear_issue_id": "ECH-888",
                "repo": REPO,
                "pr_number": 88,
                "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/88",
                "pr_base": "main",
                "branch": "ech-888-human-gate",
                "pr_head_sha": HEAD,
                "changed_files": ["service.py"],
            },
        )
        qa_id = kb.create_task(
            conn,
            title="QA",
            assignee="echlon-qa",
            parents=[implementation_id],
            created_by="echlon-coder",
            board=BOARD,
        )
        qa = kb.claim_task(conn, qa_id, claimer="qa")
        assert qa is not None and qa.current_run_id is not None
        result = hr.advance_linear_pr_after_qa(
            conn,
            qa_task_id=qa_id,
            expected_run_id=qa.current_run_id,
            approval_packet=_packet(implementation_id),
            pr_snapshot=_snapshot(),
            board=BOARD,
            worker_session_id="qa-session",
        )
    return {"db_path": db_path, "gate_id": result.gate_id, "task_id": result.task_id}


def test_fake_outbox_sends_each_destination_once_and_reaches_awaiting_human(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    with kb.connect(gate["db_path"]) as conn:
        first = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
        )
        assert set(first.sent) == hr.VALID_DELIVERY_CHANNELS
        assert first.failed == ()
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "awaiting_human"
        assert {record.payload["operation"] for record in adapter.records} == {
            "comment",
            "notify",
            "request_review",
        }

        second = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
        )
        assert second.sent == ()
        assert len(adapter.records) == 3

    for forbidden in ("merge", "push", "update_branch", "enable_auto_merge"):
        assert not hasattr(adapter, forbidden)


def test_partial_failure_retries_only_failed_destination(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    adapter.plan("slack", "error", "success")
    started = int(time.time())
    with kb.connect(gate["db_path"]) as conn:
        first = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
            now=started,
        )
        assert first.failed == ("slack",)
        assert len(adapter.records) == 2
        rows = {item.channel: item for item in hr.list_gate_deliveries(conn, gate["gate_id"])}
        assert rows["github_comment"].state == "sent"
        assert rows["github_review_request"].state == "sent"
        assert rows["slack"].state == "retry"

        second = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
            now=started + delivery.RETRY_DELAY_SECONDS + 1,
        )
        assert second.sent == ("slack",)
        assert len(adapter.records) == 3
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "awaiting_human"


def test_timeout_after_send_uses_marker_readback_without_duplicate(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    adapter.plan("github_comment", "timeout_after_send")
    with kb.connect(gate["db_path"]) as conn:
        result = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
        )
        assert "github_comment" in result.deduplicated
        assert len(adapter.records) == 3
        assert all(row.state == "sent" for row in hr.list_gate_deliveries(conn, gate["gate_id"]))


def test_head_drift_suppresses_all_unsent_deliveries_once(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    with kb.connect(gate["db_path"]) as conn:
        first = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(head_sha="e" * 40),
        )
        assert first.superseded is True
        assert adapter.records == []
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "superseded"
        task = kb.get_task(conn, gate["task_id"])
        assert task is not None and task.status == "archived"
        assert kb.delete_archived_task(conn, gate["task_id"]) is False
        assert kb.delete_task(conn, gate["task_id"]) is False
        events = [
            event
            for event in kb.list_events(conn, gate["task_id"])
            if event.kind == "human_gate_superseded"
        ]
        assert len(events) == 1

        second = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(head_sha="e" * 40),
        )
        assert second.superseded is False
        events = [
            event
            for event in kb.list_events(conn, gate["task_id"])
            if event.kind == "human_gate_superseded"
        ]
        assert len(events) == 1


def test_unavailable_snapshot_retries_without_superseding(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    with kb.connect(gate["db_path"]) as conn:
        result = delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(verified_at=1),
        )
        assert set(result.failed) == hr.VALID_DELIVERY_CHANNELS
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "pending_delivery"
        assert {row.state for row in hr.list_gate_deliveries(conn, gate["gate_id"])} == {"retry"}
        assert adapter.records == []


def test_slack_ack_is_seen_only_and_github_review_is_authoritative(gate):
    adapter = delivery.FakeReviewDeliveryAdapter()
    with kb.connect(gate["db_path"]) as conn:
        delivery.process_fake_gate_outbox(
            conn,
            gate["gate_id"],
            adapter=adapter,
            snapshot_provider=lambda _packet: _snapshot(),
        )
        assert delivery.record_fake_slack_ack(
            conn,
            gate["gate_id"],
            actor_principal="slack:U0AA6S8RX5M",
            text="approved",
            pr_snapshot=_snapshot(),
        ) is False
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "awaiting_human"
        with pytest.raises(ValueError, match="Slack acknowledgement principal"):
            delivery.record_fake_slack_ack(
                conn,
                gate["gate_id"],
                actor_principal="slack:impostor",
                text=f"ACK {gate['gate_id']}",
                pr_snapshot=_snapshot(),
            )
        assert delivery.record_fake_slack_ack(
            conn,
            gate["gate_id"],
            actor_principal="slack:U0AA6S8RX5M",
            text=f"ACK {gate['gate_id']}",
            pr_snapshot=_snapshot(),
        ) is True
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        task = kb.get_task(conn, gate["task_id"])
        assert stored is not None and stored.state == "seen"
        assert task is not None and task.status == "awaiting_human"

        with pytest.raises(ValueError, match="principal"):
            delivery.reconcile_fake_github_review(
                conn,
                gate["gate_id"],
                reviewer_principal="github:impostor",
                review_state="APPROVED",
                review_head_sha=HEAD,
                external_review_id="review-impostor",
                pr_snapshot=_snapshot(),
            )
        with pytest.raises(ValueError, match="approved gate head"):
            delivery.reconcile_fake_github_review(
                conn,
                gate["gate_id"],
                reviewer_principal="github:p-echlon",
                review_state="APPROVED",
                review_head_sha="f" * 40,
                external_review_id="review-old-head",
                pr_snapshot=_snapshot(),
            )
        assert delivery.reconcile_fake_github_review(
            conn,
            gate["gate_id"],
            reviewer_principal="github:p-echlon",
            review_state="APPROVED",
            review_head_sha=HEAD,
            external_review_id="review-current-head",
            pr_snapshot=_snapshot(),
        ) is True
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        task = kb.get_task(conn, gate["task_id"])
        assert stored is not None and stored.state == "human_approved"
        assert task is not None and task.status == "done"


def test_observed_merge_without_current_approval_is_audit_only(gate):
    with kb.connect(gate["db_path"]) as conn:
        merged = _snapshot(state="MERGED")
        assert delivery.reconcile_fake_pr_terminal_state(
            conn,
            gate["gate_id"],
            pr_snapshot=merged,
        ) is True
        stored = hr.get_human_review_gate(conn, gate["gate_id"])
        assert stored is not None and stored.state == "merged"
        task = kb.get_task(conn, gate["task_id"])
        assert task is not None and task.status == "done"
        kinds = [event.kind for event in kb.list_events(conn, gate["task_id"])]
        assert kinds.count("human_gate_merged") == 1
        assert kinds.count("merged_without_current_human_approval") == 1
        assert delivery.reconcile_fake_pr_terminal_state(
            conn,
            gate["gate_id"],
            pr_snapshot=merged,
        ) is False
        kinds = [event.kind for event in kb.list_events(conn, gate["task_id"])]
        assert kinds.count("merged_without_current_human_approval") == 1


def test_processor_rejects_non_fake_adapter_even_if_it_has_send_methods(gate):
    class PretendLiveAdapter(delivery.FakeReviewDeliveryAdapter):
        pass

    with kb.connect(gate["db_path"]) as conn:
        with pytest.raises(TypeError, match="test-only"):
            delivery.process_fake_gate_outbox(
                conn,
                gate["gate_id"],
                adapter=PretendLiveAdapter(),
                snapshot_provider=lambda _packet: _snapshot(),
            )
