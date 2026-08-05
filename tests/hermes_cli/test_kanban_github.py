from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as gh


REPO = "NousResearch/hermes-agent"
CANONICAL_REPO = "nousresearch/hermes-agent"
PR_NUMBER = 79683
HEAD_A = "a" * 40
HEAD_B = "b" * 40
NOW = 1_800_000_000


class FakeGitHubTransport:
    """In-memory readback and restricted delivery transport for tests only."""

    def __init__(self, snapshot: gh.GitHubPullRequestSnapshot):
        self.snapshot = snapshot
        self.read_calls: list[tuple[str, int]] = []
        self.send_calls: list[str] = []
        self.records: dict[str, gh.GitHubDeliveryReceipt] = {}
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
    ) -> gh.GitHubDeliveryReceipt | None:
        return self.records.get(idempotency_key)

    def send_intent(self, intent: gh.GitHubOutboxIntent) -> gh.GitHubDeliveryReceipt:
        self.send_calls.append(intent.idempotency_key)
        existing = self.records.get(intent.idempotency_key)
        if existing is not None:
            return existing
        planned = self.planned_outcomes.get(intent.operation)
        outcome = planned.pop(0) if planned else "success"
        if outcome == "retryable":
            raise gh.GitHubTransportFailure(
                "temporary fake transport failure",
                kind="network",
            )
        if outcome == "permanent":
            raise gh.GitHubTransportFailure(
                "fake reviewer permission denied",
                kind="permission",
            )

        receipt = gh.GitHubDeliveryReceipt(
            external_id=f"fake-github-{self.next_id}",
            idempotency_key=intent.idempotency_key,
        )
        self.next_id += 1
        self.records[intent.idempotency_key] = receipt
        if outcome == "timeout_after_send":
            raise gh.GitHubTransportFailure(
                "fake timeout after provider accepted the intent",
                kind="timeout",
            )
        return receipt


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "kanban.db"
    with kb.connect(path):
        pass
    return path


def _check(
    name: str = "tests",
    *,
    check_id: str = "check-tests",
    head_sha: str = HEAD_A,
    status: gh.CheckStatus = "completed",
    conclusion: gh.CheckConclusion | None = "success",
) -> gh.GitHubCheck:
    return gh.GitHubCheck(
        check_id=check_id,
        name=name,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
    )


def _review(
    review_id: str,
    *,
    author_login: str = "p-echlon",
    head_sha: str = HEAD_A,
    state: gh.ReviewState = "approved",
    submitted_at: int = NOW,
) -> gh.GitHubReview:
    return gh.GitHubReview(
        review_id=review_id,
        author_login=author_login,
        head_sha=head_sha,
        state=state,
        submitted_at=submitted_at,
    )


def _comment(
    comment_id: str = "comment-1",
    *,
    head_sha: str = HEAD_A,
    actionable: bool = True,
) -> gh.GitHubReviewComment:
    return gh.GitHubReviewComment(
        comment_id=comment_id,
        author_login="reviewer",
        head_sha=head_sha,
        created_at=NOW,
        actionable=actionable,
    )


def _thread(
    thread_id: str = "thread-1",
    *,
    head_sha: str = HEAD_A,
    resolved: bool = False,
    outdated: bool = False,
    actionable: bool = True,
    comments: tuple[gh.GitHubReviewComment, ...] = (),
) -> gh.GitHubReviewThread:
    return gh.GitHubReviewThread(
        thread_id=thread_id,
        head_sha=head_sha,
        resolved=resolved,
        outdated=outdated,
        actionable=actionable,
        comments=comments,
    )


def _snapshot(
    observation_id: str = "snapshot-a",
    *,
    head_sha: str = HEAD_A,
    state: gh.PullRequestState = "open",
    is_draft: bool = False,
    observed_at: int = NOW,
    checks: tuple[gh.GitHubCheck, ...] = (),
    reviews: tuple[gh.GitHubReview, ...] = (),
    review_threads: tuple[gh.GitHubReviewThread, ...] = (),
    requested_reviewers: tuple[gh.GitHubRequestedReviewer, ...] = (),
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
        checks=checks,
        reviews=reviews,
        review_threads=review_threads,
        requested_reviewers=requested_reviewers,
    )


def _payload(operation: gh.GitHubOperation) -> dict[str, object]:
    if operation == "request_reviewer":
        return {"reviewer_principal": "github:p-echlon", "gate_id": "g_test"}
    if operation == "create_comment":
        return {
            "body": "QA approved this exact head for human review.",
            "marker": "<!-- exact-head:g_test -->",
            "gate_id": "g_test",
        }
    return {"reviewer_principal": "github:p-echlon", "gate_id": "g_test"}


def _enqueue(
    conn,
    *,
    snapshot: gh.GitHubPullRequestSnapshot,
    operation: gh.GitHubOperation = "request_reviewer",
    surface: gh.GitHubSurface = "review_requests",
    payload: dict[str, object] | None = None,
) -> gh.EnqueueReceipt:
    return gh.enqueue_intent(
        conn,
        gate_id="g_test",
        snapshot=snapshot,
        expected_repository=REPO,
        expected_pr_number=PR_NUMBER,
        expected_head_sha=snapshot.head_sha,
        surface=surface,
        operation=operation,
        payload=payload or _payload(operation),
        now=NOW,
    )


def test_snapshot_protocol_captures_full_readback_and_human_decisions(db_path: Path):
    comment = _comment()
    snapshot = _snapshot(
        checks=(_check(),),
        reviews=(
            _review("review-commented", state="commented", submitted_at=NOW - 1),
            _review("review-approved", state="approved", submitted_at=NOW),
        ),
        review_threads=(_thread(comments=(comment,)),),
        requested_reviewers=(
            gh.GitHubRequestedReviewer(principal="p-echlon", kind="user"),
        ),
    )
    provider: gh.GitHubSnapshotProvider = FakeGitHubTransport(snapshot)

    observed = provider.read_snapshot(repository=REPO, pr_number=PR_NUMBER)
    decisions = gh.read_human_review_decisions(
        observed,
        expected_head_sha=HEAD_A,
        reviewer_login="p-echlon",
    )

    assert observed.repository == CANONICAL_REPO
    assert observed.base_ref == "main"
    assert observed.head_ref == "echlon-coder/t_2e33818f-srdja-gate"
    assert observed.requested_reviewers[0].principal == "p-echlon"
    assert observed.review_threads[0].comments == (comment,)
    assert [(item.state, item.review_id) for item in decisions] == [
        ("approved", "review-approved"),
    ]
    assert observed.to_human_review_mapping()["source"] == "github_readback"

    with kb.connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {"github_human_review_outbox", "github_human_review_attempts"} <= tables
    assert {
        "uq_github_outbox_semantic_identity",
        "uq_github_outbox_idempotency_key",
        "idx_github_outbox_due",
        "uq_github_attempt_number",
    } <= indexes


def test_snapshot_head_mismatch_is_stale_and_closed_or_merged_cannot_enqueue(
    db_path: Path,
):
    current = _snapshot(checks=(_check(),))
    stale = _snapshot(head_sha=HEAD_B)
    with pytest.raises(gh.GitHubHeadMismatch, match="exact head"):
        gh.validate_exact_head(stale, expected_head_sha=HEAD_A)
    assert (
        gh.assess_review_readiness(
            stale,
            expected_head_sha=HEAD_A,
            coderabbit_state="clean",
        ).state
        == "stale"
    )

    with kb.connect(db_path) as conn:
        existing = _enqueue(conn, snapshot=current).intent
        with pytest.raises(gh.GitHubHeadMismatch, match="exact head"):
            gh.enqueue_intent(
                conn,
                gate_id="g_stale",
                snapshot=stale,
                expected_repository=REPO,
                expected_pr_number=PR_NUMBER,
                expected_head_sha=HEAD_A,
                surface="review_requests",
                operation="request_reviewer",
                payload=_payload("request_reviewer"),
                now=NOW,
            )
        superseded = gh.get_intent(conn, existing.id)
        assert superseded is not None and superseded.state == "superseded"
        for state in ("closed", "merged"):
            with pytest.raises(gh.GitHubPRTerminal, match=state):
                _enqueue(conn, snapshot=_snapshot(state=state))
        assert (
            conn.execute("SELECT COUNT(*) FROM github_human_review_outbox").fetchone()[
                0
            ]
            == 1
        )


def test_green_checks_do_not_override_coderabbit_or_human_review_evidence():
    green = _snapshot(checks=(_check(),))
    assert (
        gh.assess_review_readiness(
            green,
            expected_head_sha=HEAD_A,
            coderabbit_state="clean",
        ).state
        == "ready"
    )

    coderabbit_blocked = gh.assess_review_readiness(
        green,
        expected_head_sha=HEAD_A,
        coderabbit_state="actionable",
    )
    assert coderabbit_blocked.state == "blocked"
    assert "coderabbit_actionable" in coderabbit_blocked.reasons

    human_blocked = gh.assess_review_readiness(
        replace(
            green,
            reviews=(_review("review-changes", state="changes_requested"),),
        ),
        expected_head_sha=HEAD_A,
        coderabbit_state="clean",
    )
    assert human_blocked.state == "blocked"
    assert "human_changes_requested" in human_blocked.reasons

    thread_blocked = gh.assess_review_readiness(
        replace(green, review_threads=(_thread(),)),
        expected_head_sha=HEAD_A,
        coderabbit_state="clean",
    )
    assert thread_blocked.state == "blocked"
    assert "actionable_review_thread" in thread_blocked.reasons


def test_outbox_deduplicates_identical_intent_and_allows_new_head_generation(
    db_path: Path,
):
    snapshot_a = _snapshot(checks=(_check(),))
    snapshot_b = _snapshot(
        observation_id="snapshot-b",
        head_sha=HEAD_B,
        observed_at=NOW + 1,
        checks=(_check(head_sha=HEAD_B),),
    )

    with kb.connect(db_path) as conn:
        first = _enqueue(conn, snapshot=snapshot_a)
        duplicate = _enqueue(conn, snapshot=snapshot_a)
        assert first.created is True
        assert duplicate.created is False
        assert duplicate.intent.id == first.intent.id

        with pytest.raises(gh.GitHubReplayConflict, match="different payload"):
            _enqueue(
                conn,
                snapshot=snapshot_a,
                payload={"reviewer_principal": "github:someone-else"},
            )

        next_head = gh.enqueue_intent(
            conn,
            gate_id="g_next",
            snapshot=snapshot_b,
            expected_repository=REPO,
            expected_pr_number=PR_NUMBER,
            expected_head_sha=HEAD_B,
            surface="review_requests",
            operation="request_reviewer",
            payload={"reviewer_principal": "github:p-echlon", "gate_id": "g_next"},
            now=NOW + 1,
        )
        assert next_head.created is True
        rows = gh.list_intents(conn, repository=REPO, pr_number=PR_NUMBER)
        assert [(row.head_sha, row.state) for row in rows] == [
            (HEAD_A, "superseded"),
            (HEAD_B, "pending"),
        ]
        assert rows[0].idempotency_key != rows[1].idempotency_key


def test_fake_transport_sends_notification_review_request_and_comment_once(
    db_path: Path,
):
    snapshot = _snapshot(
        checks=(_check(),),
        requested_reviewers=(
            gh.GitHubRequestedReviewer(principal="p-echlon", kind="user"),
        ),
        review_threads=(_thread(comments=(_comment(),), actionable=False),),
    )
    transport = FakeGitHubTransport(snapshot)
    operations: tuple[tuple[gh.GitHubSurface, gh.GitHubOperation], ...] = (
        ("pull_request", "notify_human_review"),
        ("review_requests", "request_reviewer"),
        ("pull_request_comments", "create_comment"),
    )

    with kb.connect(db_path) as conn:
        intents = [
            gh.enqueue_intent(
                conn,
                gate_id="g_test",
                snapshot=snapshot,
                expected_repository=REPO,
                expected_pr_number=PR_NUMBER,
                expected_head_sha=HEAD_A,
                surface=surface,
                operation=operation,
                payload=_payload(operation),
                now=NOW,
            ).intent
            for surface, operation in operations
        ]
        results = [
            gh.process_intent(
                conn,
                intent.id,
                snapshot_provider=transport,
                delivery_transport=transport,
                now=NOW,
            )
            for intent in intents
        ]
        assert [result.outcome for result in results] == [
            "sent",
            "requested_reviewer_present",
            "sent",
        ]
        assert {
            row.operation
            for row in gh.list_intents(conn, repository=REPO, pr_number=PR_NUMBER)
        } == {
            "notify_human_review",
            "request_reviewer",
            "create_comment",
        }
        repeat = gh.process_intent(
            conn,
            intents[-1].id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW + 1,
        )
        assert repeat.outcome == "already_terminal"
        assert len(transport.send_calls) == 2
        assert len(transport.records) == 2

    for forbidden in ("merge", "approve", "update_branch", "enable_auto_merge"):
        assert not hasattr(transport, forbidden)


def test_stale_head_supersedes_intent_before_transport_send(db_path: Path):
    snapshot_a = _snapshot(checks=(_check(),))
    snapshot_b = _snapshot(
        observation_id="snapshot-b",
        head_sha=HEAD_B,
        observed_at=NOW + 1,
        checks=(_check(head_sha=HEAD_B),),
    )
    transport = FakeGitHubTransport(snapshot_b)

    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot_a).intent
        result = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW + 1,
        )
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "head_superseded"
        assert result.superseded is True
        assert stored is not None and stored.state == "superseded"
        assert transport.send_calls == []


def test_retryable_failure_retries_then_succeeds_and_respects_due_time(db_path: Path):
    snapshot = _snapshot(checks=(_check(),))
    transport = FakeGitHubTransport(snapshot)
    transport.plan("request_reviewer", "retryable", "success")

    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        first = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        assert first.outcome == "retry_scheduled"
        assert first.retryable is True
        retrying = gh.get_intent(conn, intent.id)
        assert retrying is not None
        assert retrying.state == "retry"
        assert retrying.next_attempt_at == NOW + gh.DEFAULT_RETRY_DELAY_SECONDS

        too_early = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=retrying.next_attempt_at - 1,
        )
        assert too_early.outcome == "not_due"
        assert len(transport.send_calls) == 1

        sent = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=retrying.next_attempt_at,
        )
        assert sent.outcome == "sent"
        stored = gh.get_intent(conn, intent.id)
        assert stored is not None and stored.state == "sent"
        assert stored.attempt_count == 2
        assert len(gh.list_attempts(conn, intent.id)) == 2


def test_stale_readback_retries_before_any_outbound_send(db_path: Path):
    current = _snapshot(checks=(_check(),))
    stale = replace(current, observation_id="snapshot-stale", observed_at=NOW - 301)
    transport = FakeGitHubTransport(stale)

    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=current).intent
        result = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "retry_scheduled"
        assert result.retryable is True
        assert stored is not None and stored.state == "retry"
        assert stored.last_failure_kind == "unavailable"
        assert transport.send_calls == []


def test_default_transport_is_disabled_and_fails_closed(db_path: Path):
    snapshot = _snapshot(checks=(_check(),))
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        result = gh.process_intent(conn, intent.id, now=NOW)
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "permanent_failure"
        assert result.retryable is False
        assert stored is not None and stored.state == "permanent_failure"
        assert stored.last_failure_kind == "disabled"


def test_expired_attempt_lease_uses_provider_readback_before_replay(db_path: Path):
    snapshot = _snapshot(checks=(_check(),))
    transport = FakeGitHubTransport(snapshot)
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        transport.records[intent.idempotency_key] = gh.GitHubDeliveryReceipt(
            external_id="fake-existing-delivery",
            idempotency_key=intent.idempotency_key,
        )
        conn.execute(
            "UPDATE github_human_review_outbox SET state='attempting', "
            "attempt_count=1, updated_at=? WHERE id=?",
            (NOW - gh.ATTEMPT_LEASE_SECONDS - 1, intent.id),
        )

        result = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "already_delivered"
        assert result.deduplicated is True
        assert stored is not None and stored.state == "sent"
        assert stored.attempt_count == 2
        assert transport.send_calls == []
        assert [item.outcome for item in gh.list_attempts(conn, intent.id)] == [
            "attempt_lease_expired",
            "already_delivered",
        ]


def test_timeout_after_send_uses_idempotent_readback_without_replay(db_path: Path):
    snapshot = _snapshot(checks=(_check(),))
    transport = FakeGitHubTransport(snapshot)
    transport.plan("create_comment", "timeout_after_send")

    with kb.connect(db_path) as conn:
        intent = _enqueue(
            conn,
            snapshot=snapshot,
            surface="pull_request_comments",
            operation="create_comment",
        ).intent
        result = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "sent_after_readback"
        assert result.deduplicated is True
        assert stored is not None and stored.state == "sent"
        assert len(transport.send_calls) == 1
        assert len(transport.records) == 1


def test_permanent_failure_is_terminal_and_failure_classification_is_deterministic(
    db_path: Path,
):
    retryable = {
        kind
        for kind in ("network", "timeout", "rate_limited", "server", "unavailable")
        if gh.classify_transport_failure(
            gh.GitHubTransportFailure("failure", kind=kind)
        ).retryable
    }
    assert retryable == {"network", "timeout", "rate_limited", "server", "unavailable"}
    for kind in (
        "disabled",
        "auth",
        "permission",
        "not_found",
        "validation",
        "conflict",
    ):
        assert (
            gh.classify_transport_failure(
                gh.GitHubTransportFailure("failure", kind=kind)
            ).retryable
            is False
        )
    assert gh.classify_transport_failure(RuntimeError("unknown")).kind == "unknown"
    assert gh.classify_transport_failure(RuntimeError("unknown")).retryable is False

    snapshot = _snapshot(checks=(_check(),))
    transport = FakeGitHubTransport(snapshot)
    transport.plan("request_reviewer", "permanent")
    with kb.connect(db_path) as conn:
        intent = _enqueue(conn, snapshot=snapshot).intent
        result = gh.process_intent(
            conn,
            intent.id,
            snapshot_provider=transport,
            delivery_transport=transport,
            now=NOW,
        )
        stored = gh.get_intent(conn, intent.id)
        assert result.outcome == "permanent_failure"
        assert result.retryable is False
        assert stored is not None and stored.state == "permanent_failure"
        assert stored.last_failure_kind == "permission"
        assert (
            gh.process_intent(
                conn,
                intent.id,
                snapshot_provider=transport,
                delivery_transport=transport,
                now=NOW + 1,
            ).outcome
            == "already_terminal"
        )
        assert len(transport.send_calls) == 1


def test_human_decision_readback_is_latest_exact_head_evidence_only():
    snapshot = _snapshot(
        checks=(_check(),),
        reviews=(
            _review("old-approval", state="approved", submitted_at=NOW - 10),
            _review("current-changes", state="changes_requested", submitted_at=NOW),
            _review(
                "other-reviewer-changes",
                author_login="other-reviewer",
                state="changes_requested",
                submitted_at=NOW - 2,
            ),
            _review(
                "other-reviewer-dismissed",
                author_login="other-reviewer",
                state="dismissed",
                submitted_at=NOW - 1,
            ),
            _review(
                "stale-head-approval",
                head_sha=HEAD_B,
                state="approved",
                submitted_at=NOW + 1,
            ),
        ),
    )

    decisions = gh.read_human_review_decisions(
        snapshot,
        expected_head_sha=HEAD_A,
        reviewer_login="p-echlon",
    )

    assert len(decisions) == 1
    assert decisions[0].state == "changes_requested"
    assert decisions[0].head_sha == HEAD_A
    assert [
        item.reviewer_login
        for item in gh.read_human_review_decisions(
            snapshot,
            expected_head_sha=HEAD_A,
        )
    ] == ["p-echlon"]
    assert (
        gh.assess_review_readiness(
            snapshot,
            expected_head_sha=HEAD_A,
            coderabbit_state="clean",
        ).state
        == "blocked"
    )
