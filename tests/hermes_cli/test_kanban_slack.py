from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as gh
from hermes_cli import kanban_slack as slack


REPO = "NousResearch/hermes-agent"
CANONICAL_REPO = "nousresearch/hermes-agent"
PR_NUMBER = 79683
HEAD_A = "a" * 40
HEAD_B = "b" * 40
CHANNEL = "C0BMC4GBGJH"
NOW = 1_800_000_000


class FakeSlackTransport:
    """In-memory exact-route Slack transport and PR readback for tests only."""

    def __init__(self, snapshot: gh.GitHubPullRequestSnapshot):
        self.snapshot = snapshot
        self.read_calls: list[tuple[str, int]] = []
        self.send_calls: list[slack.SlackOutboxIntent] = []
        self.records: dict[str, slack.SlackDeliveryReceipt] = {}
        self.planned_outcomes: dict[str, list[str]] = {}
        self.next_id = 1

    def plan(self, operation: str, *outcomes: str) -> None:
        self.planned_outcomes.setdefault(operation, []).extend(outcomes)

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> gh.GitHubPullRequestSnapshot:
        self.read_calls.append((repository, pr_number))
        return self.snapshot

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> slack.SlackDeliveryReceipt | None:
        return self.records.get(idempotency_key)

    def send_intent(
        self,
        intent: slack.SlackOutboxIntent,
    ) -> slack.SlackDeliveryReceipt:
        self.send_calls.append(intent)
        existing = self.records.get(intent.idempotency_key)
        if existing is not None:
            return existing
        planned = self.planned_outcomes.get(intent.operation)
        outcome = planned.pop(0) if planned else "success"
        if outcome == "not_in_channel":
            raise slack.SlackTransportFailure(
                "fake bot is not a channel member",
                kind="not_in_channel",
            )
        if outcome == "permission":
            raise slack.SlackTransportFailure(
                "fake Slack permission denied",
                kind="permission",
            )
        if outcome == "rate_limited":
            raise slack.SlackTransportFailure(
                "fake Slack rate limit",
                kind="rate_limited",
                retry_after_seconds=120,
            )
        if outcome == "transient":
            raise slack.SlackTransportFailure(
                "fake transient Slack failure",
                kind="transient",
            )
        if outcome == "channel_not_found":
            raise slack.SlackTransportFailure(
                "fake channel missing",
                kind="channel_not_found",
            )

        message_ts = f"1800000000.{self.next_id:06d}"
        thread_ts = intent.thread_ts or message_ts
        receipt = slack.SlackDeliveryReceipt(
            external_id=f"fake-slack-{self.next_id}",
            message_ts=message_ts,
            thread_ts=thread_ts,
            idempotency_key=intent.idempotency_key,
        )
        self.next_id += 1
        if outcome == "wrong_thread":
            return replace(receipt, thread_ts="1800000000.999999")
        self.records[intent.idempotency_key] = receipt
        if outcome == "timeout_after_send":
            raise slack.SlackTransportFailure(
                "fake timeout after Slack accepted the message",
                kind="timeout",
            )
        return receipt


class FakeAcknowledgementProvider:
    def __init__(self, *events: slack.SlackAcknowledgementEvent):
        self.events = tuple(events)
        self.calls: list[tuple[str, str]] = []

    def read_acknowledgements(
        self,
        *,
        channel_id: str,
        thread_ts: str,
    ) -> tuple[slack.SlackAcknowledgementEvent, ...]:
        self.calls.append((channel_id, thread_ts))
        return self.events


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "kanban.db"
    with kb.connect(path) as conn:
        _insert_gate(conn)
    return path


def _check(head_sha: str = HEAD_A) -> gh.GitHubCheck:
    return gh.GitHubCheck(
        check_id=f"check-{head_sha[:8]}",
        name="tests",
        head_sha=head_sha,
        status="completed",
        conclusion="success",
    )


def _snapshot(
    observation_id: str = "snapshot-a",
    *,
    head_sha: str = HEAD_A,
    state: gh.PullRequestState = "open",
    is_draft: bool = False,
    observed_at: int = NOW,
) -> gh.GitHubPullRequestSnapshot:
    return gh.GitHubPullRequestSnapshot(
        provider="GitHub",
        observation_id=observation_id,
        repository=REPO,
        pr_number=PR_NUMBER,
        pr_url=f"https://github.com/{REPO}/pull/{PR_NUMBER}",
        state=state,
        is_draft=is_draft,
        base_ref="main",
        head_ref="echlon-coder/t_2e33818f-srdja-gate",
        head_sha=head_sha,
        observed_at=observed_at,
        checks=(_check(head_sha),),
    )


def _enqueue(
    conn,
    *,
    snapshot: gh.GitHubPullRequestSnapshot,
    gate_id: str = "g_test",
    channel_id: str = CHANNEL,
    thread_ts: str | None = None,
    surface: slack.SlackSurface = "channel",
    operation: slack.SlackOperation = "notify_human_review",
    source_intent_id: str | None = None,
    body: str = "QA approved this exact head for human review in GitHub.",
    now: int = NOW,
) -> slack.EnqueueReceipt:
    return slack.enqueue_intent(
        conn,
        gate_id=gate_id,
        snapshot=snapshot,
        expected_repository=REPO,
        expected_pr_number=PR_NUMBER,
        expected_head_sha=snapshot.head_sha,
        channel_id=channel_id,
        thread_ts=thread_ts,
        surface=surface,
        operation=operation,
        payload={"body": body, "gate_id": gate_id},
        source_intent_id=source_intent_id,
        now=now,
    )


def _send_notification(
    conn,
    snapshot: gh.GitHubPullRequestSnapshot,
    transport: FakeSlackTransport,
) -> slack.SlackOutboxIntent:
    intent = _enqueue(conn, snapshot=snapshot).intent
    result = slack.process_intent(
        conn,
        intent.id,
        snapshot_provider=transport,
        delivery_transport=transport,
        now=snapshot.observed_at,
    )
    assert result.outcome == "sent"
    stored = slack.get_intent(conn, intent.id)
    assert stored is not None
    return stored


def _ack_event(
    thread_ts: str,
    *,
    event_id: str = "Ev-ack-1",
    source: slack.AcknowledgementSource = "text",
    value: str = "approved",
    observed_at: int = NOW + 1,
) -> slack.SlackAcknowledgementEvent:
    return slack.SlackAcknowledgementEvent(
        provider="Slack",
        event_id=event_id,
        channel_id=CHANNEL,
        thread_ts=thread_ts,
        message_ts=f"{thread_ts}-reply",
        user_id="U0AA6S8RX5M",
        source=source,
        value=value,
        observed_at=observed_at,
    )


def _insert_gate(conn, *, gate_id: str = "g_test") -> None:
    conn.execute(
        """
        INSERT INTO human_review_gates (
            id, task_id, schema_version, gate_kind, reviewer_principal,
            notification_principal, repo, pr_number, pr_url, linear_issue_id,
            base_branch, head_branch, approved_head_sha, implementation_task_id,
            qa_task_id, qa_run_id, qa_worker_session_id, qa_verdict,
            qa_attempt_count, coder_correction_attempt_count, qa_approved_at,
            approval_packet_json, approval_packet_sha256, state,
            superseded_by_gate_id, created_at, updated_at
        ) VALUES (
            ?, 't_human', 1, 'srdja_pr_review', 'github:p-echlon',
            'slack:U0AA6S8RX5M', ?, ?, ?, 'ECH-999', 'main', 'feature', ?,
            't_impl', 't_qa', 1, 'qa-session', 'APPROVE_FOR_SRDJA_REVIEW',
            0, 0, ?, '{}', ?, 'awaiting_human', NULL, ?, ?
        )
        """,
        (
            gate_id,
            CANONICAL_REPO,
            PR_NUMBER,
            f"https://github.com/{REPO}/pull/{PR_NUMBER}",
            HEAD_A,
            NOW,
            "f" * 64,
            NOW,
            NOW,
        ),
    )


def test_schema_and_provider_neutral_protocols_are_additive(db_path: Path):
    snapshot = _snapshot()
    transport: slack.PullRequestSnapshotProvider = FakeSlackTransport(snapshot)
    delivery: slack.SlackDeliveryTransport = transport
    acknowledgement: slack.SlackAcknowledgementProvider = FakeAcknowledgementProvider()

    assert transport.read_snapshot(repository=REPO, pr_number=PR_NUMBER) is snapshot
    assert delivery.find_delivery(idempotency_key="missing") is None
    assert acknowledgement.read_acknowledgements(
        channel_id=CHANNEL,
        thread_ts="1800000000.000001",
    ) == ()

    with kb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {
        "slack_human_review_outbox",
        "slack_human_review_attempts",
        "slack_human_review_acknowledgements",
    } <= tables
    assert {
        "uq_slack_outbox_semantic_identity",
        "uq_slack_outbox_idempotency_key",
        "idx_slack_outbox_due",
        "uq_slack_attempt_number",
        "uq_slack_ack_provider_event",
        "uq_slack_ack_semantic_replay",
        "idx_slack_ack_thread",
    } <= indexes
    for forbidden in (
        "approve",
        "merge",
        "invite_to_channel",
        "join_channel",
        "archive_channel",
    ):
        assert not hasattr(delivery, forbidden)
        assert not hasattr(acknowledgement, forbidden)


def test_identical_intents_deduplicate_and_new_head_is_distinct(db_path: Path):
    snapshot_a = _snapshot()
    snapshot_b = _snapshot(
        "snapshot-b",
        head_sha=HEAD_B,
        observed_at=NOW + 1,
    )
    with kb.connect(db_path) as conn:
        first = _enqueue(conn, snapshot=snapshot_a)
        duplicate = _enqueue(conn, snapshot=snapshot_a)
        assert first.created is True
        assert duplicate.created is False
        assert duplicate.intent.id == first.intent.id

        with pytest.raises(slack.SlackReplayConflict, match="different payload"):
            _enqueue(conn, snapshot=snapshot_a, body="different body")

        next_head = _enqueue(
            conn,
            snapshot=snapshot_b,
            gate_id="g_next",
            now=NOW + 1,
        )
        assert next_head.created is True
        rows = slack.list_intents(conn, repository=REPO, pr_number=PR_NUMBER)
        assert [(row.head_sha, row.state) for row in rows] == [
            (HEAD_A, "superseded"),
            (HEAD_B, "pending"),
        ]
        assert rows[0].idempotency_key != rows[1].idempotency_key


def test_top_level_send_and_thread_reply_preserve_explicit_route(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        top = _send_notification(conn, snapshot, transport)
        assert top.thread_ts == ""
        assert top.delivered_thread_ts == top.external_message_ts
        assert transport.send_calls[0].channel_id == CHANNEL
        assert transport.send_calls[0].thread_ts == ""

        reply = _enqueue(
            conn,
            snapshot=snapshot,
            thread_ts=top.delivered_thread_ts,
            surface="thread",
            operation="reply",
            source_intent_id=top.id,
            body="Acknowledgement recorded; review remains in GitHub.",
        ).intent
        sent = slack.process_intent(
            conn,
            reply.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored_reply = slack.get_intent(conn, reply.id)
        assert sent.outcome == "sent"
        assert stored_reply is not None
        assert stored_reply.thread_ts == top.delivered_thread_ts
        assert stored_reply.delivered_thread_ts == top.delivered_thread_ts
        assert transport.send_calls[-1].thread_ts == top.delivered_thread_ts
        assert (
            slack.process_intent(
                conn,
                reply.id,
                snapshot_provider=transport,
                delivery_transport=transport,
                now=NOW + 1,
            ).outcome
            == "already_terminal"
        )

    with pytest.raises(slack.SlackBoundaryError, match="require stored thread_ts"):
        with kb.connect(db_path) as conn:
            _enqueue(
                conn,
                snapshot=snapshot,
                surface="thread",
                operation="reply",
            )
    with pytest.raises(slack.SlackBoundaryError, match="sent source notification"):
        with kb.connect(db_path) as conn:
            _enqueue(
                conn,
                snapshot=snapshot,
                thread_ts="1800000000.999999",
                surface="thread",
                operation="reply",
            )


def test_top_level_receipt_cannot_invent_a_different_thread_root(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    transport.plan("notify_human_review", "wrong_thread")
    with kb.connect(db_path) as conn:
        intent = _enqueue(
            conn,
            snapshot=snapshot,
            channel_id="CWRONGROOT",
        ).intent
        result = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = slack.get_intent(conn, intent.id)
        assert result.outcome == "permanent_failure"
        assert stored is not None
        assert stored.last_failure_kind == "conflict"


def test_transport_cannot_change_stored_thread_route(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        top = _send_notification(conn, snapshot, transport)
        transport.plan("reply", "wrong_thread")
        reply = _enqueue(
            conn,
            snapshot=snapshot,
            thread_ts=top.delivered_thread_ts,
            surface="thread",
            operation="reply",
            source_intent_id=top.id,
            body="Stored-thread reply",
        ).intent
        result = slack.process_intent(
            conn,
            reply.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = slack.get_intent(conn, reply.id)
        assert result.outcome == "permanent_failure"
        assert stored is not None
        assert stored.last_failure_kind == "conflict"


def test_stale_head_supersedes_unsent_intent_before_slack_send(db_path: Path):
    snapshot_a = _snapshot()
    snapshot_b = _snapshot(
        "snapshot-b",
        head_sha=HEAD_B,
        observed_at=NOW + 1,
    )
    transport = FakeSlackTransport(snapshot_b)
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot_a).intent
        result = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW + 1,
        )
        stored = slack.get_intent(conn, intent.id)
        assert result.outcome == "head_superseded"
        assert result.superseded is True
        assert stored is not None and stored.state == "superseded"
        assert transport.send_calls == []


def test_terminal_human_gate_suppresses_notification_before_send(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        conn.execute(
            "UPDATE human_review_gates SET state='closed' WHERE id='g_test'"
        )
        result = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = slack.get_intent(conn, intent.id)
        assert result.outcome == "human_gate_closed"
        assert result.superseded is True
        assert stored is not None and stored.state == "superseded"
        assert transport.send_calls == []


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_closed_or_merged_pr_suppresses_notification_before_send(
    db_path: Path,
    state: gh.PullRequestState,
):
    current = _snapshot()
    terminal = _snapshot(f"snapshot-{state}", state=state, observed_at=NOW + 1)
    transport = FakeSlackTransport(terminal)
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=current).intent
        result = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW + 1,
        )
        assert result.outcome == f"pull_request_{state}"
        assert result.superseded is True
        assert transport.send_calls == []


def test_acknowledgement_is_replay_safe_and_never_approves_gate(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        sent = _send_notification(conn, snapshot, transport)
        assert sent.delivered_thread_ts is not None
        event = _ack_event(sent.delivered_thread_ts, value="approved")

        first = slack.record_acknowledgement(
            conn,
            source_intent_id=sent.id,
            event=event,
            now=NOW + 1,
        )
        duplicate = slack.record_acknowledgement(
            conn,
            source_intent_id=sent.id,
            event=event,
            now=NOW + 2,
        )
        assert first.created is True
        assert first.receipt.normalized_action == "acknowledged"
        assert first.receipt.acknowledged is True
        assert duplicate.created is False
        assert duplicate.receipt.id == first.receipt.id
        assert len(slack.list_acknowledgements(conn, source_intent_id=sent.id)) == 1
        gate_state = conn.execute(
            "SELECT state FROM human_review_gates WHERE id='g_test'"
        ).fetchone()[0]
        assert gate_state == "awaiting_human"

        conflicting = replace(event, value="merge it")
        with pytest.raises(slack.SlackReplayConflict, match="event ID was reused"):
            slack.record_acknowledgement(
                conn,
                source_intent_id=sent.id,
                event=conflicting,
                now=NOW + 3,
            )

        semantic_replay = replace(
            event,
            event_id="Ev-ack-redelivery",
            observed_at=NOW + 99,
        )
        replay = slack.record_acknowledgement(
            conn,
            source_intent_id=sent.id,
            event=semantic_replay,
            now=NOW + 99,
        )
        assert replay.created is False
        assert replay.receipt.id == first.receipt.id


def test_acknowledgement_normalization_and_route_binding(db_path: Path):
    assert slack.normalize_acknowledgement_action("reaction", ":eyes:") == "viewed"
    assert (
        slack.normalize_acknowledgement_action("reaction", "white_check_mark")
        == "acknowledged"
    )
    assert slack.normalize_acknowledgement_action("text", "will review") == "will_review"
    assert slack.normalize_acknowledgement_action("text", "LGTM") == "acknowledged"
    assert slack.normalize_acknowledgement_action("button", "merge") == "acknowledged"
    assert slack.normalize_acknowledgement_action("text", "unrelated prose") == "ignored"
    assert "approved" not in slack.ACKNOWLEDGEMENT_ACTIONS

    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        sent = _send_notification(conn, snapshot, transport)
        assert sent.delivered_thread_ts is not None
        wrong_route = replace(
            _ack_event(sent.delivered_thread_ts),
            thread_ts="1800000000.999999",
        )
        with pytest.raises(slack.SlackBoundaryError, match="stored thread_ts"):
            slack.record_acknowledgement(
                conn,
                source_intent_id=sent.id,
                event=wrong_route,
                now=NOW + 1,
            )


def test_not_in_channel_and_permission_are_permanent_failures(db_path: Path):
    snapshot = _snapshot()
    for index, failure in enumerate(("not_in_channel", "permission"), start=1):
        transport = FakeSlackTransport(snapshot)
        transport.plan("notify_human_review", failure)
        with kb.connect(db_path) as conn:
            intent = _enqueue(
                conn,
                snapshot=snapshot,
                channel_id=f"CFAIL{index}",
            ).intent
            result = slack.process_intent(
                conn,
                intent.id,
                snapshot_provider=transport,
                delivery_transport=transport,
                now=NOW,
            )
            stored = slack.get_intent(conn, intent.id)
            assert result.outcome == "permanent_failure"
            assert result.retryable is False
            assert stored is not None
            assert stored.last_failure_kind == failure


@pytest.mark.parametrize("failure", ["rate_limited", "transient"])
def test_rate_limit_and_transient_failures_retry_then_send(
    db_path: Path,
    failure: str,
):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    transport.plan("notify_human_review", failure, "success")
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        first = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        retrying = slack.get_intent(conn, intent.id)
        assert first.outcome == "retry_scheduled"
        assert first.retryable is True
        assert retrying is not None
        expected_delay = 120 if failure == "rate_limited" else 30
        assert retrying.next_attempt_at == NOW + expected_delay
        assert retrying.next_attempt_at is not None

        too_early = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=retrying.next_attempt_at - 1,
        )
        assert too_early.outcome == "not_due"
        assert len(transport.send_calls) == 1

        sent = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=retrying.next_attempt_at,
        )
        stored = slack.get_intent(conn, intent.id)
        assert sent.outcome == "sent"
        assert stored is not None and stored.state == "sent"
        assert stored.attempt_count == 2
        assert len(slack.list_attempts(conn, intent.id)) == 2


def test_timeout_after_send_uses_idempotent_readback_without_replay(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    transport.plan("notify_human_review", "timeout_after_send")
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        result = slack.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = slack.get_intent(conn, intent.id)
        assert result.outcome == "sent_after_readback"
        assert result.deduplicated is True
        assert stored is not None and stored.state == "sent"
        assert len(transport.send_calls) == 1
        assert len(transport.records) == 1


def test_disabled_default_is_fail_closed_and_permanent(db_path: Path):
    snapshot = _snapshot()
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        result = slack.process_intent(conn, intent.id, now=NOW)
        stored = slack.get_intent(conn, intent.id)
        assert result.outcome == "permanent_failure"
        assert result.retryable is False
        assert stored is not None and stored.state == "permanent_failure"
        assert stored.last_failure_kind == "disabled"

    with pytest.raises(slack.SlackTransportFailure, match="disabled"):
        slack.DisabledSlackAcknowledgementProvider().read_acknowledgements(
            channel_id=CHANNEL,
            thread_ts="1800000000.000001",
        )


def test_failure_classification_is_typed_and_deterministic():
    retryable = {
        kind
        for kind in (
            "rate_limited",
            "transient",
            "network",
            "timeout",
            "server",
            "unavailable",
        )
        if slack.classify_transport_failure(
            slack.SlackTransportFailure("failure", kind=kind)
        ).retryable
    }
    assert retryable == {
        "rate_limited",
        "transient",
        "network",
        "timeout",
        "server",
        "unavailable",
    }
    for kind in (
        "disabled",
        "not_in_channel",
        "permission",
        "auth",
        "channel_not_found",
        "thread_not_found",
        "validation",
        "conflict",
    ):
        assert (
            slack.classify_transport_failure(
                slack.SlackTransportFailure("failure", kind=kind)
            ).retryable
            is False
        )
    unknown = slack.classify_transport_failure(RuntimeError("unknown"))
    assert unknown.kind == "unknown"
    assert unknown.retryable is False


def test_payload_cannot_override_stored_channel_or_thread(db_path: Path):
    snapshot = _snapshot()
    with kb.connect(db_path) as conn:
        with pytest.raises(slack.SlackBoundaryError, match="immutable outbox fields"):
            slack.enqueue_intent(
                conn,
                gate_id="g_test",
                snapshot=snapshot,
                expected_repository=REPO,
                expected_pr_number=PR_NUMBER,
                expected_head_sha=HEAD_A,
                channel_id=CHANNEL,
                thread_ts=None,
                surface="channel",
                operation="notify_human_review",
                payload={"body": "message", "channel_id": "CGUESSED"},
                now=NOW,
            )


def test_persisted_payload_hash_is_verified_before_send(db_path: Path):
    snapshot = _snapshot()
    transport = FakeSlackTransport(snapshot)
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        conn.execute(
            "UPDATE slack_human_review_outbox SET payload_json=? WHERE id=?",
            ('{"body":"tampered"}', intent.id),
        )
        with pytest.raises(slack.SlackReplayConflict, match="payload hash"):
            slack.process_intent(
                conn,
                intent.id,
                snapshot_provider=transport,
                delivery_transport=transport,
                now=NOW,
            )
        assert transport.send_calls == []
