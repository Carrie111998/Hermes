"""Cross-system exact-head reconciliation tests with fake-only source state."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
from hermes_cli import kanban_linear as linear
from hermes_cli import kanban_reconciliation as reconciliation
from hermes_cli import kanban_slack as slack


NOW = 1_900_000_000
ISSUE_ID = "linear-issue-uuid-reconciliation"
IDENTIFIER = "ECH-999"
REPO = "echlon-bank/echlon-bank"
PR_NUMBER = 999
HEAD_A = "a" * 40
HEAD_B = "b" * 40
GATE_ID = "g_reconcile"
CHANNEL = "C0BMC4GBGJH"


class FakeSnapshotProvider:
    """Read-only in-memory GitHub snapshot provider for reconciliation tests."""

    def __init__(
        self,
        snapshots: dict[tuple[str, int], github.GitHubPullRequestSnapshot | None],
    ) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, int]] = []

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> github.GitHubPullRequestSnapshot | None:
        self.calls.append((repository, pr_number))
        return self.snapshots.get((repository, pr_number))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(
    *,
    head_sha: str = HEAD_A,
    state: github.PullRequestState = "open",
    repository: str = REPO,
    pr_number: int = PR_NUMBER,
    observation_id: str = "github-observation-a",
) -> github.GitHubPullRequestSnapshot:
    return github.GitHubPullRequestSnapshot(
        provider="github",
        observation_id=observation_id,
        repository=repository,
        pr_number=pr_number,
        pr_url=f"https://github.com/{repository}/pull/{pr_number}",
        state=state,
        is_draft=False,
        base_ref="main",
        head_ref="ech-999-reconciliation",
        head_sha=head_sha,
        observed_at=NOW,
        checks=(
            github.GitHubCheck(
                check_id=f"check-{head_sha[:8]}",
                name="tests",
                head_sha=head_sha,
                status="completed",
                conclusion="success",
            ),
        ),
    )


def _github_intent(
    intent_id: str,
    *,
    operation: github.GitHubOperation,
    head_sha: str = HEAD_A,
    gate_id: str = GATE_ID,
) -> github.GitHubOutboxIntent:
    surface: github.GitHubSurface = (
        "pull_request_comments" if operation == "create_comment" else "review_requests"
    )
    payload = {"gate_id": gate_id, "body": f"fake {operation}"}
    return github.GitHubOutboxIntent(
        id=intent_id,
        gate_id=gate_id,
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=head_sha,
        surface=surface,
        operation=operation,
        payload=payload,
        payload_sha256=_digest(str(sorted(payload.items()))),
        idempotency_key=f"github:{gate_id}:{operation}:{head_sha}",
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        external_id=None,
        last_snapshot_sha256=None,
        last_snapshot_observed_at=None,
        last_failure_kind=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        sent_at=None,
    )


def _slack_intent(
    intent_id: str = "slo_reconcile",
    *,
    gate_id: str = GATE_ID,
    head_sha: str = HEAD_A,
) -> slack.SlackOutboxIntent:
    payload = {"gate_id": gate_id, "body": "fake exact-head notification"}
    return slack.SlackOutboxIntent(
        id=intent_id,
        gate_id=gate_id,
        source_intent_id=None,
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=head_sha,
        channel_id=CHANNEL,
        thread_ts="",
        surface="channel",
        operation="notify_human_review",
        payload=payload,
        payload_sha256=_digest(str(sorted(payload.items()))),
        idempotency_key=f"slack:{gate_id}:{CHANNEL}:{head_sha}",
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=None,
        external_message_ts=None,
        delivered_thread_ts=None,
        last_snapshot_sha256=None,
        last_snapshot_observed_at=None,
        last_failure_kind=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        sent_at=None,
    )


def _healthy_inputs() -> reconciliation.ReconciliationInputs:
    ref = linear.PullRequestRef(REPO, PR_NUMBER)
    snapshot = _snapshot()
    aggregate = linear.PullRequestAggregate(
        ref=ref,
        pr_url=snapshot.pr_url,
        state="open",
        is_draft=False,
        base_branch="main",
        head_branch="ech-999-reconciliation",
        current_head_sha=HEAD_A,
        provider_revision=7,
        snapshot_sha256=_digest("linear-pr-snapshot"),
        observed_at=NOW,
        updated_at=NOW,
    )
    assessment = coderabbit.CodeRabbitAssessment(
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=HEAD_A,
        review_generation=2,
        observed_at=NOW,
        state="clean",
        reason="typed_clean_summary_for_current_head",
        actionable_count=0,
        unresolved_count=0,
        resolved_count=0,
        outdated_count=0,
        superseded_count=0,
        non_blocking_count=0,
        actionable_finding_ids=(),
        snapshot_sha256=_digest("coderabbit-clean"),
        correction=coderabbit.CorrectionMetadata(
            correction_work_key=(
                f"coderabbit-correction:v1:{REPO}:pr:{PR_NUMBER}:head:{HEAD_A}"
            ),
            loop_prevention_key=_digest("coderabbit-loop"),
            attempt_count=0,
            max_attempts=1,
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    gate = human_review.HumanReviewGate(
        id=GATE_ID,
        task_id="t_human",
        schema_version=1,
        gate_kind="srdja_pr_review",
        reviewer_principal="github:p-echlon",
        notification_principal="slack:U0AA6S8RX5M",
        repo=REPO,
        pr_number=PR_NUMBER,
        pr_url=snapshot.pr_url,
        linear_issue_id=ISSUE_ID,
        base_branch="main",
        head_branch="ech-999-reconciliation",
        approved_head_sha=HEAD_A,
        implementation_task_id="t_impl",
        qa_task_id="t_qa",
        qa_run_id=1,
        qa_worker_session_id="qa-session",
        qa_verdict="APPROVE_FOR_SRDJA_REVIEW",
        qa_attempt_count=0,
        coder_correction_attempt_count=0,
        qa_approved_at=NOW,
        approval_packet={"coderabbit": {"status": "clean"}},
        approval_packet_sha256=_digest("approval-packet"),
        state="awaiting_human",
        superseded_by_gate_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    deliveries = (
        human_review.ReviewGateDelivery(
            gate_id=GATE_ID,
            channel="github_comment",
            destination=snapshot.pr_url,
            state="pending",
            attempt_count=0,
            next_attempt_at=None,
            external_id=None,
            dedupe_marker="delivery:github-comment",
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        human_review.ReviewGateDelivery(
            gate_id=GATE_ID,
            channel="github_review_request",
            destination="github:p-echlon",
            state="pending",
            attempt_count=0,
            next_attempt_at=None,
            external_id=None,
            dedupe_marker="delivery:github-review-request",
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
        ),
        human_review.ReviewGateDelivery(
            gate_id=GATE_ID,
            channel="slack",
            destination="slack:U0AA6S8RX5M",
            state="pending",
            attempt_count=0,
            next_attempt_at=None,
            external_id=None,
            dedupe_marker="delivery:slack",
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    return reconciliation.ReconciliationInputs(
        coordinators=(
            linear.LinearIssueCoordinator(
                issue_id=ISSUE_ID,
                identifier=IDENTIFIER,
                title="Implement exact-head reconciliation",
                issue_url=("https://linear.app/echlon/issue/ECH-999/reconciliation"),
                source_revision=7,
                snapshot_sha256=_digest("linear-issue-snapshot"),
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
        links=(
            reconciliation.LinearIssuePullRequestLink(
                linear_issue_id=ISSUE_ID,
                ref=reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                first_seen_revision=1,
                last_seen_revision=7,
            ),
        ),
        stored_pr_aggregates=(aggregate,),
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                snapshot,
            ),
        ),
        coderabbit_heads=(
            reconciliation.CodeRabbitHeadPointer(
                ref=reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                current_head_sha=HEAD_A,
                observed_at=NOW,
            ),
        ),
        coderabbit_assessments=(assessment,),
        task_states=(
            reconciliation.KanbanTaskState("t_human", "awaiting_human", "srdja", None),
            reconciliation.KanbanTaskState("t_impl", "done", "echlon-coder", 1),
            reconciliation.KanbanTaskState("t_qa", "done", "echlon-qa", 2),
        ),
        human_gates=(gate,),
        gate_deliveries=deliveries,
        github_intents=(
            _github_intent("gho_comment", operation="create_comment"),
            _github_intent("gho_reviewer", operation="request_reviewer"),
        ),
        slack_intents=(_slack_intent(),),
    )


@pytest.fixture
def healthy_inputs() -> reconciliation.ReconciliationInputs:
    return _healthy_inputs()


@pytest.fixture
def stale_head_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    current = _snapshot(
        head_sha=HEAD_B,
        observation_id="github-observation-b",
    )
    return replace(
        healthy_inputs,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                current,
            ),
        ),
    )


@pytest.fixture
def closed_pr_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    closed = _snapshot(state="closed", observation_id="github-observation-closed")
    return replace(
        healthy_inputs,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                closed,
            ),
        ),
    )


@pytest.fixture
def missing_coderabbit_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    return replace(
        healthy_inputs,
        coderabbit_heads=(),
        coderabbit_assessments=(),
    )


@pytest.fixture
def duplicate_orphan_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    duplicate = replace(_slack_intent(), id="slo_duplicate")
    orphan = _slack_intent(
        "slo_orphan",
        gate_id="g_missing",
    )
    return replace(
        healthy_inputs,
        slack_intents=healthy_inputs.slack_intents + (duplicate, orphan),
    )


@pytest.fixture
def missing_slack_delivery_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    return replace(healthy_inputs, slack_intents=())


@pytest.fixture
def conflicting_identity_inputs(
    healthy_inputs: reconciliation.ReconciliationInputs,
) -> reconciliation.ReconciliationInputs:
    wrong = _snapshot(
        pr_number=PR_NUMBER + 1,
        observation_id="github-observation-wrong-pr",
    )
    return replace(
        healthy_inputs,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                wrong,
            ),
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    path = home / "kanban" / "boards" / "echlon-linear-fixes" / "kanban.db"
    kb.init_db(path)
    return path


def _categories(report: reconciliation.ReconciliationReport) -> set[str]:
    return {finding.category for finding in report.findings}


def test_healthy_report_is_deterministic_provider_neutral_and_action_free(
    healthy_inputs: reconciliation.ReconciliationInputs,
):
    first = reconciliation.build_reconciliation_report(healthy_inputs)
    second = reconciliation.build_reconciliation_report(healthy_inputs)

    assert first == second
    assert first.to_json() == second.to_json()
    assert first.to_markdown() == second.to_markdown()
    assert first.report_sha256() == second.report_sha256()
    assert first.status == "healthy"
    assert _categories(first) == {"current_gates"}
    current = first.findings[0]
    assert current.expected_head_sha == HEAD_A
    assert current.observed_head_sha == HEAD_A
    assert current.entity_ids == (GATE_ID, "t_human", "t_qa")
    assert current.key.startswith("rcf_")
    assert current.to_dict()["external_write_permitted"] is False
    assert current.to_dict()["automatic_action"] == "none"
    assert first.to_dict()["safety"] == {
        "mode": "read_only_audit_recommendation",
        "automatic_actions": [],
        "external_writes": False,
        "board_mutation": False,
        "merge": False,
        "approval": False,
        "notification": False,
        "migration": False,
    }
    for forbidden in (
        "merge",
        "approve",
        "notify",
        "migrate",
        "post_to_slack",
        "advance_gate",
    ):
        assert not hasattr(reconciliation, forbidden)


def test_current_head_mismatch_wins_over_stale_approval_and_evidence(
    stale_head_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(stale_head_inputs)

    assert report.status == "blocked"
    stale = [
        finding for finding in report.findings if finding.category == "stale_heads"
    ]
    assert stale
    assert any(finding.code == "active_gate_head_is_stale" for finding in stale)
    assert all(finding.observed_head_sha == HEAD_B for finding in stale)
    assert "current_gates" not in _categories(report)
    assert "actionable_coderabbit_findings" not in _categories(report)
    missing = [
        finding
        for finding in report.findings
        if finding.code == "current_head_missing_coderabbit_evidence"
    ]
    assert len(missing) == 1
    assert missing[0].observed_head_sha == HEAD_B


def test_closed_pr_is_terminal_and_never_current(
    closed_pr_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(closed_pr_inputs)

    assert report.status == "blocked"
    assert "terminal_prs" in _categories(report)
    assert "current_gates" not in _categories(report)
    assert any(
        finding.code == "active_gate_on_closed_pr"
        and finding.expected_head_sha == HEAD_A
        for finding in report.findings
    )
    assert "missing_qa_evidence" not in _categories(report)


def test_missing_coderabbit_evidence_is_an_exact_head_qa_gap(
    missing_coderabbit_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(missing_coderabbit_inputs)

    missing = [
        finding
        for finding in report.findings
        if finding.code == "current_head_missing_coderabbit_evidence"
    ]
    assert report.status == "needs_attention"
    assert len(missing) == 1
    assert missing[0].source == "coderabbit"
    assert missing[0].repository == REPO
    assert missing[0].pr_number == PR_NUMBER
    assert missing[0].observed_head_sha == HEAD_A


def test_actionable_coderabbit_findings_block_current_head_advancement(
    healthy_inputs: reconciliation.ReconciliationInputs,
):
    clean = healthy_inputs.coderabbit_assessments[0]
    actionable = replace(
        clean,
        state="actionable",
        actionable_count=1,
        unresolved_count=1,
        actionable_finding_ids=("coderabbit-finding-1",),
        snapshot_sha256=_digest("coderabbit-actionable"),
    )
    inputs = replace(
        healthy_inputs,
        coderabbit_assessments=(actionable,),
    )

    report = reconciliation.build_reconciliation_report(inputs)

    finding = next(
        item
        for item in report.findings
        if item.code == "current_head_has_actionable_coderabbit_findings"
    )
    assert report.status == "blocked"
    assert finding.category == "actionable_coderabbit_findings"
    assert finding.severity == "critical"
    assert finding.expected_head_sha == HEAD_A
    assert finding.observed_head_sha == HEAD_A
    assert "coderabbit-finding-1" in finding.entity_ids
    assert finding.to_dict()["automatic_action"] == "none"


def test_duplicate_and_orphan_rows_are_bucketed_without_repair(
    duplicate_orphan_inputs: reconciliation.ReconciliationInputs,
):
    original_ids = tuple(intent.id for intent in duplicate_orphan_inputs.slack_intents)
    report = reconciliation.build_reconciliation_report(duplicate_orphan_inputs)

    assert report.status == "needs_attention"
    assert "duplicate_semantic_rows" in _categories(report)
    assert "orphaned_records" in _categories(report)
    assert any(
        finding.code == "duplicate_slack_outbox_intent" for finding in report.findings
    )
    assert any(
        finding.code == "slack_outbox_without_gate" for finding in report.findings
    )
    assert (
        tuple(intent.id for intent in duplicate_orphan_inputs.slack_intents)
        == original_ids
    )


def test_missing_slack_delivery_is_reported_but_not_created(
    missing_slack_delivery_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(missing_slack_delivery_inputs)

    finding = next(
        item
        for item in report.findings
        if item.code == "missing_slack_outbox_notification"
    )
    assert finding.category == "missing_outbox_rows"
    assert finding.source == "slack"
    assert finding.expected_head_sha == HEAD_A
    assert missing_slack_delivery_inputs.slack_intents == ()


def test_conflicting_linear_github_identity_fails_closed(
    conflicting_identity_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(conflicting_identity_inputs)

    conflict = next(
        item
        for item in report.findings
        if item.code == "linear_github_identity_conflict"
    )
    assert report.status == "blocked"
    assert conflict.category == "conflicting_source_revisions"
    assert conflict.severity == "critical"
    assert conflict.repository == REPO
    assert conflict.pr_number == PR_NUMBER
    assert "current_gates" not in _categories(report)


def test_idempotent_run_persists_machine_and_human_reports_only(
    db_path: Path,
    healthy_inputs: reconciliation.ReconciliationInputs,
):
    report = reconciliation.build_reconciliation_report(healthy_inputs)
    source_tables = (
        "linear_issue_coordinators",
        "linear_issue_pr_links",
        "linear_pr_aggregates",
        "coderabbit_pr_heads",
        "coderabbit_head_assessments",
        "human_review_gates",
        "review_gate_deliveries",
        "github_human_review_outbox",
        "slack_human_review_outbox",
        "slack_human_review_acknowledgements",
    )
    with kb.connect(db_path) as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in source_tables
        }
        first = reconciliation.record_reconciliation_run(conn, report, now=NOW)
        second = reconciliation.record_reconciliation_run(
            conn,
            report,
            now=NOW + 60,
        )
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in source_tables
        }
        row = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE id=?",
            (first.run_id,),
        ).fetchone()
        finding_rows = conn.execute(
            "SELECT finding_key, finding_json FROM reconciliation_findings "
            "WHERE run_id=? ORDER BY finding_key",
            (first.run_id,),
        ).fetchall()
        indexes = {
            item["name"]
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }

    assert first.created is True
    assert second.created is False
    assert first.run_id == second.run_id
    assert first.report_sha256 == report.report_sha256()
    assert before == after
    assert row is not None
    assert row["report_json"] == report.to_json()
    assert row["report_markdown"] == report.to_markdown()
    assert len(finding_rows) == len(report.findings)
    assert {item["finding_key"] for item in finding_rows} == {
        finding.key for finding in report.findings
    }
    assert {
        "uq_reconciliation_run_input",
        "idx_reconciliation_run_status",
        "idx_reconciliation_finding_category",
    } <= indexes


def test_default_reconcile_is_read_only_and_resolves_current_head_via_fake_github(
    db_path: Path,
):
    stored_snapshot = _snapshot(head_sha=HEAD_A)
    current_snapshot = _snapshot(
        head_sha=HEAD_B,
        observation_id="github-live-b",
    )
    issue_digest = _digest("issue")
    aggregate_digest = _digest("aggregate")
    provider = FakeSnapshotProvider({(REPO, PR_NUMBER): current_snapshot})

    with kb.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO linear_issue_coordinators (
                linear_issue_id, linear_identifier, title, issue_url,
                source_revision, snapshot_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                ISSUE_ID,
                IDENTIFIER,
                "Reconcile exact head",
                "https://linear.app/echlon/issue/ECH-999/reconciliation",
                issue_digest,
                NOW,
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO linear_issue_pr_links (
                linear_issue_id, repository, pr_number, first_seen_revision,
                last_seen_revision, created_at, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?, ?)
            """,
            (ISSUE_ID, REPO, PR_NUMBER, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO linear_pr_aggregates (
                repository, pr_number, pr_url, state, is_draft, base_branch,
                head_branch, current_head_sha, provider_revision,
                snapshot_sha256, observed_at, updated_at
            ) VALUES (?, ?, ?, 'open', 0, 'main', ?, ?, 1, ?, ?, ?)
            """,
            (
                REPO,
                PR_NUMBER,
                stored_snapshot.pr_url,
                stored_snapshot.head_ref,
                HEAD_A,
                aggregate_digest,
                NOW,
                NOW,
            ),
        )
        before = conn.total_changes
        execution = reconciliation.reconcile(
            conn,
            snapshot_provider=provider,
            persist=False,
            now=NOW,
        )
        after = conn.total_changes
        run_count = conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[
            0
        ]
        stored_head = conn.execute(
            "SELECT current_head_sha FROM linear_pr_aggregates "
            "WHERE repository=? AND pr_number=?",
            (REPO, PR_NUMBER),
        ).fetchone()[0]

    assert provider.calls == [(REPO, PR_NUMBER)]
    assert execution.persisted_run is None
    assert execution.report.status == "blocked"
    assert any(
        finding.code == "stored_pr_head_is_stale"
        for finding in execution.report.findings
    )
    assert run_count == 0
    assert before == after
    assert stored_head == HEAD_A
