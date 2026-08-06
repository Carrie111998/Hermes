"""Exact-head review migration planning and local checkpoint tests."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
from hermes_cli import kanban_linear as linear
from hermes_cli import kanban_reconciliation as reconciliation
from hermes_cli import kanban_review_migration as migration
from hermes_cli import kanban_slack as slack


NOW = 1_900_100_000
ISSUE_ID = "linear-review-migration"
REPO = "echlon-bank/echlon-bank"
PR_NUMBER = 701
HEAD_A = "a" * 40
HEAD_B = "b" * 40
GATE_ID = "g_review_migration"
TASK_ID = "t_review_migration_human"
CHANNEL = "C0BMC4GBGJH"


class FakeSnapshotProvider:
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
    observation_id: str = "github-migration-observation",
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
        head_ref="ech-701-review-migration",
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


def _gate(
    *, gate_id: str = GATE_ID, task_id: str = TASK_ID
) -> human_review.HumanReviewGate:
    return human_review.HumanReviewGate(
        id=gate_id,
        task_id=task_id,
        schema_version=1,
        gate_kind="srdja_pr_review",
        reviewer_principal="github:p-echlon",
        notification_principal="slack:U0AA6S8RX5M",
        repo=REPO,
        pr_number=PR_NUMBER,
        pr_url=f"https://github.com/{REPO}/pull/{PR_NUMBER}",
        linear_issue_id=ISSUE_ID,
        base_branch="main",
        head_branch="ech-701-review-migration",
        approved_head_sha=HEAD_A,
        implementation_task_id="t_impl",
        qa_task_id="t_qa",
        qa_run_id=4,
        qa_worker_session_id="qa-session",
        qa_verdict="APPROVE_FOR_SRDJA_REVIEW",
        qa_attempt_count=0,
        coder_correction_attempt_count=0,
        qa_approved_at=NOW,
        approval_packet={"coderabbit": {"status": "clean"}},
        approval_packet_sha256=_digest("migration-approval-packet"),
        state="awaiting_human",
        superseded_by_gate_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _delivery(channel: str) -> human_review.ReviewGateDelivery:
    destinations = {
        "github_comment": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
        "github_review_request": "github:p-echlon",
        "slack": "slack:U0AA6S8RX5M",
    }
    return human_review.ReviewGateDelivery(
        gate_id=GATE_ID,
        channel=channel,
        destination=destinations[channel],
        state="pending",
        attempt_count=0,
        next_attempt_at=None,
        external_id=None,
        dedupe_marker=f"fixture:{channel}",
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _github_intent(
    intent_id: str,
    operation: github.GitHubOperation,
) -> github.GitHubOutboxIntent:
    surface: github.GitHubSurface = (
        "pull_request_comments" if operation == "create_comment" else "review_requests"
    )
    payload = {"gate_id": GATE_ID, "body": f"fixture {operation}"}
    return github.GitHubOutboxIntent(
        id=intent_id,
        gate_id=GATE_ID,
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=HEAD_A,
        surface=surface,
        operation=operation,
        payload=payload,
        payload_sha256=_digest(json.dumps(payload, sort_keys=True)),
        idempotency_key=f"github:{GATE_ID}:{operation}:{HEAD_A}",
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=NOW + 60,
        external_id=None,
        last_snapshot_sha256=None,
        last_snapshot_observed_at=None,
        last_failure_kind=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        sent_at=None,
    )


def _slack_intent() -> slack.SlackOutboxIntent:
    payload = {"gate_id": GATE_ID, "body": "fixture Slack notification"}
    return slack.SlackOutboxIntent(
        id="slo_review_migration",
        gate_id=GATE_ID,
        source_intent_id=None,
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=HEAD_A,
        channel_id=CHANNEL,
        thread_ts="",
        surface="channel",
        operation="notify_human_review",
        payload=payload,
        payload_sha256=_digest(json.dumps(payload, sort_keys=True)),
        idempotency_key=f"slack:{GATE_ID}:{CHANNEL}:{HEAD_A}",
        state="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=NOW + 60,
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
    snapshot = _snapshot()
    ref = reconciliation.PullRequestIdentity(REPO, PR_NUMBER)
    aggregate = linear.PullRequestAggregate(
        ref=linear.PullRequestRef(REPO, PR_NUMBER),
        pr_url=snapshot.pr_url,
        state="open",
        is_draft=False,
        base_branch="main",
        head_branch="ech-701-review-migration",
        current_head_sha=HEAD_A,
        provider_revision=8,
        snapshot_sha256=_digest("linear-pr-migration"),
        observed_at=NOW,
        updated_at=NOW,
    )
    assessment = coderabbit.CodeRabbitAssessment(
        repository=REPO,
        pr_number=PR_NUMBER,
        head_sha=HEAD_A,
        review_generation=3,
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
        snapshot_sha256=_digest("coderabbit-migration-clean"),
        correction=coderabbit.CorrectionMetadata(
            correction_work_key=(
                f"coderabbit-correction:v1:{REPO}:pr:{PR_NUMBER}:head:{HEAD_A}"
            ),
            loop_prevention_key=_digest("migration-loop"),
            attempt_count=0,
            max_attempts=1,
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    return reconciliation.ReconciliationInputs(
        coordinators=(
            linear.LinearIssueCoordinator(
                issue_id=ISSUE_ID,
                identifier="ECH-701",
                title="Review migration fixtures",
                issue_url="https://linear.app/echlon/issue/ECH-701/review-migration",
                source_revision=8,
                snapshot_sha256=_digest("linear-issue-migration"),
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
        links=(
            reconciliation.LinearIssuePullRequestLink(
                linear_issue_id=ISSUE_ID,
                ref=ref,
                first_seen_revision=1,
                last_seen_revision=8,
            ),
        ),
        stored_pr_aggregates=(aggregate,),
        trusted_pr_reads=(reconciliation.TrustedPullRequestRead(ref, "ok", snapshot),),
        coderabbit_heads=(reconciliation.CodeRabbitHeadPointer(ref, HEAD_A, NOW),),
        coderabbit_assessments=(assessment,),
        task_states=(
            reconciliation.KanbanTaskState(TASK_ID, "awaiting_human", "srdja", None),
            reconciliation.KanbanTaskState("t_impl", "done", "echlon-coder", 1),
            reconciliation.KanbanTaskState("t_qa", "done", "echlon-qa", 2),
        ),
        human_gates=(_gate(),),
        gate_deliveries=tuple(
            _delivery(channel) for channel in human_review.DEFAULT_DELIVERY_CHANNELS
        ),
        github_intents=(
            _github_intent("gho_migration_comment", "create_comment"),
            _github_intent("gho_migration_reviewer", "request_reviewer"),
        ),
        slack_intents=(_slack_intent(),),
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    path = home / "kanban.db"
    kb.init_db(path)
    return path


def _plan(
    source: reconciliation.ReconciliationInputs,
    *,
    legacy_tasks: tuple[reconciliation.KanbanTaskState, ...] = (),
) -> migration.MigrationPlan:
    return migration.build_migration_plan(
        migration.MigrationInputs(source, legacy_tasks)
    )


def _record(
    plan: migration.MigrationPlan,
    entity_type: str,
    entity_id: str,
) -> migration.MigrationRecord:
    return next(
        item
        for item in plan.records
        if item.entity_type == entity_type and item.entity_id == entity_id
    )


def _seed_gate_bundle(
    conn,
    source: reconciliation.ReconciliationInputs,
) -> None:
    gate = source.human_gates[0]
    for task in source.task_states:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task.task_id, f"Fixture {task.task_id}", task.assignee, task.status, NOW),
        )
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gate.id,
            gate.task_id,
            gate.schema_version,
            gate.gate_kind,
            gate.reviewer_principal,
            gate.notification_principal,
            gate.repo,
            gate.pr_number,
            gate.pr_url,
            gate.linear_issue_id,
            gate.base_branch,
            gate.head_branch,
            gate.approved_head_sha,
            gate.implementation_task_id,
            gate.qa_task_id,
            gate.qa_run_id,
            gate.qa_worker_session_id,
            gate.qa_verdict,
            gate.qa_attempt_count,
            gate.coder_correction_attempt_count,
            gate.qa_approved_at,
            json.dumps(gate.approval_packet, sort_keys=True),
            gate.approval_packet_sha256,
            gate.state,
            gate.superseded_by_gate_id,
            gate.created_at,
            gate.updated_at,
        ),
    )
    for item in source.gate_deliveries:
        conn.execute(
            """
            INSERT INTO review_gate_deliveries (
                gate_id, channel, destination, state, attempt_count,
                next_attempt_at, external_id, dedupe_marker, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.gate_id,
                item.channel,
                item.destination,
                item.state,
                item.attempt_count,
                item.next_attempt_at,
                item.external_id,
                item.dedupe_marker,
                item.last_error,
                item.created_at,
                item.updated_at,
            ),
        )
    for item in source.github_intents:
        conn.execute(
            """
            INSERT INTO github_human_review_outbox (
                id, gate_id, repository, pr_number, head_sha, surface,
                operation, payload_json, payload_sha256, idempotency_key,
                state, attempt_count, max_attempts, next_attempt_at,
                external_id, last_snapshot_sha256, last_snapshot_observed_at,
                last_failure_kind, last_error, created_at, updated_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.gate_id,
                item.repository,
                item.pr_number,
                item.head_sha,
                item.surface,
                item.operation,
                json.dumps(item.payload, sort_keys=True),
                item.payload_sha256,
                item.idempotency_key,
                item.state,
                item.attempt_count,
                item.max_attempts,
                item.next_attempt_at,
                item.external_id,
                item.last_snapshot_sha256,
                item.last_snapshot_observed_at,
                item.last_failure_kind,
                item.last_error,
                item.created_at,
                item.updated_at,
                item.sent_at,
            ),
        )
    for item in source.slack_intents:
        conn.execute(
            """
            INSERT INTO slack_human_review_outbox (
                id, gate_id, source_intent_id, repository, pr_number, head_sha,
                channel_id, thread_ts, surface, operation, payload_json,
                payload_sha256, idempotency_key, state, attempt_count,
                max_attempts, next_attempt_at, external_message_ts,
                delivered_thread_ts, last_snapshot_sha256,
                last_snapshot_observed_at, last_failure_kind, last_error,
                created_at, updated_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.gate_id,
                item.source_intent_id,
                item.repository,
                item.pr_number,
                item.head_sha,
                item.channel_id,
                item.thread_ts,
                item.surface,
                item.operation,
                json.dumps(item.payload, sort_keys=True),
                item.payload_sha256,
                item.idempotency_key,
                item.state,
                item.attempt_count,
                item.max_attempts,
                item.next_attempt_at,
                item.external_message_ts,
                item.delivered_thread_ts,
                item.last_snapshot_sha256,
                item.last_snapshot_observed_at,
                item.last_failure_kind,
                item.last_error,
                item.created_at,
                item.updated_at,
                item.sent_at,
            ),
        )
    conn.commit()


def test_current_head_plan_is_deterministic_auditable_and_action_free() -> None:
    first = _plan(_healthy_inputs())
    second = _plan(_healthy_inputs())

    assert first.to_json() == second.to_json()
    assert first.plan_id == second.plan_id
    assert not first.blocked
    assert first.actions == ()
    assert _record(first, "human_review_gate", GATE_ID).classification == "current_head"
    payload = first.to_dict()
    assert payload["safety"]["default_mode"] == "dry-run"
    assert payload["safety"]["external_side_effects"] == "none"
    assert payload["safety"]["merge"] is False
    assert first.apply_confirmation == f"APPLY {first.plan_id}"
    assert "never infers a head from a task title" in first.to_markdown()


def test_default_cli_plan_writes_report_but_not_board_database(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at) "
            "VALUES ('t_legacy', 'Legacy prose must not be parsed', 'srdja', "
            "'awaiting_human', ?)",
            (NOW,),
        )
        conn.commit()
    before = db_path.read_bytes()
    report_path = tmp_path / "migration-dry-run.md"
    args = argparse.Namespace(
        kanban_action="review-migration",
        migration_action="plan",
        linear_issue_id=None,
        plan_id=None,
        confirm=None,
        operator=None,
        max_actions=None,
        report=report_path,
        json=False,
    )

    assert kanban_cli.kanban_command(args) == 0

    captured = capsys.readouterr()
    assert "Legacy prose must not be parsed" not in captured.out
    assert "External side effects: none" in report_path.read_text()
    assert db_path.read_bytes() == before
    with kb.connect_closing(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM review_migration_plans").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot(head_sha=HEAD_B, observation_id="stale-head"), "stale"),
        (_snapshot(state="closed", observation_id="closed-pr"), "terminal"),
        (_snapshot(state="merged", observation_id="merged-pr"), "terminal"),
    ],
)
def test_stale_and_terminal_snapshots_propose_local_suppression(
    snapshot: github.GitHubPullRequestSnapshot,
    expected: migration.Classification,
) -> None:
    source = _healthy_inputs()
    source = replace(
        source,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                snapshot,
            ),
        ),
    )

    plan = _plan(source)

    gate = _record(plan, "human_review_gate", GATE_ID)
    assert gate.classification == expected
    assert any(action.kind == "suppress_gate_bundle" for action in plan.actions)
    assert all(len(action.head_sha) == 40 for action in plan.actions)
    assert all(action.rollback["mode"] == "manual" for action in plan.actions)


def test_duplicate_orphan_missing_pr_and_conflicting_sources_block() -> None:
    healthy = _healthy_inputs()
    duplicate_gate = replace(_gate(gate_id="g_duplicate", task_id="t_duplicate"))
    duplicate = replace(
        healthy,
        human_gates=healthy.human_gates + (duplicate_gate,),
        task_states=healthy.task_states
        + (
            reconciliation.KanbanTaskState(
                "t_duplicate", "awaiting_human", "srdja", None
            ),
        ),
    )
    duplicate_plan = _plan(duplicate)
    assert duplicate_plan.blocked
    assert (
        _record(duplicate_plan, "human_review_gate", GATE_ID).classification
        == "duplicate"
    )

    orphan_plan = _plan(
        healthy,
        legacy_tasks=(
            reconciliation.KanbanTaskState(
                "t_legacy_orphan", "awaiting_human", "srdja", None
            ),
        ),
    )
    orphan = _record(orphan_plan, "legacy_human_task", "t_legacy_orphan")
    assert orphan.classification == "orphan"
    assert orphan.repository is None and orphan.stored_head_sha is None
    assert orphan_plan.blocked

    missing_pr = replace(
        healthy,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "unavailable",
                None,
            ),
        ),
    )
    missing_plan = _plan(missing_pr)
    assert (
        _record(missing_plan, "human_review_gate", GATE_ID).classification == "orphan"
    )
    assert missing_plan.blocked

    wrong_identity = _snapshot(
        pr_number=PR_NUMBER + 1,
        observation_id="conflicting-source",
    )
    conflict = replace(
        healthy,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                wrong_identity,
            ),
        ),
    )
    conflict_plan = _plan(conflict)
    assert (
        _record(conflict_plan, "human_review_gate", GATE_ID).classification
        == "ambiguous"
    )
    assert conflict_plan.blocked


def test_write_confirmation_exact_head_and_idempotent_rerun(
    db_path: Path,
) -> None:
    healthy = _healthy_inputs()
    source = replace(
        healthy,
        gate_deliveries=tuple(
            item for item in healthy.gate_deliveries if item.channel != "slack"
        ),
    )
    plan = _plan(source)
    assert not plan.blocked
    assert len(plan.actions) == 1
    provider = FakeSnapshotProvider({(REPO, PR_NUMBER): _snapshot()})
    with kb.connect_closing(db_path) as conn:
        _seed_gate_bundle(conn, source)
        with pytest.raises(migration.MigrationConfirmationRequired):
            migration.apply_migration_plan(
                conn,
                plan,
                snapshot_provider=provider,
                confirmation="yes",
                operator="test-operator",
                now=NOW + 10,
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM review_migration_plans").fetchone()[0]
            == 0
        )

        first = migration.apply_migration_plan(
            conn,
            plan,
            snapshot_provider=provider,
            confirmation=plan.apply_confirmation,
            operator="test-operator",
            now=NOW + 10,
        )
        second = migration.apply_migration_plan(
            conn,
            plan,
            snapshot_provider=provider,
            confirmation=plan.apply_confirmation,
            operator="test-operator",
            now=NOW + 20,
        )
        assert first.status == "completed"
        assert len(first.applied_action_ids) == 1
        assert second.status == "completed"
        assert second.applied_action_ids == ()
        assert second.skipped_action_ids == first.applied_action_ids
        assert second.checkpoint_count == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_gate_deliveries WHERE gate_id=? AND channel='slack'",
                (GATE_ID,),
            ).fetchone()[0]
            == 1
        )
        assert migration.load_migration_plan(conn, plan.plan_id) == plan


def test_head_change_after_planning_fails_closed_before_mutation(
    db_path: Path,
) -> None:
    healthy = _healthy_inputs()
    source = replace(healthy, gate_deliveries=healthy.gate_deliveries[:-1])
    plan = _plan(source)
    changed_provider = FakeSnapshotProvider({
        (REPO, PR_NUMBER): _snapshot(head_sha=HEAD_B, observation_id="head-changed")
    })
    with kb.connect_closing(db_path) as conn:
        _seed_gate_bundle(conn, source)
        with pytest.raises(migration.MigrationConflict, match="head changed"):
            migration.apply_migration_plan(
                conn,
                plan,
                snapshot_provider=changed_provider,
                confirmation=plan.apply_confirmation,
                operator="test-operator",
                now=NOW + 10,
            )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_gate_deliveries WHERE gate_id=? AND channel='slack'",
                (GATE_ID,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT checkpoint_count FROM review_migration_plans WHERE id=?",
                (plan.plan_id,),
            ).fetchone()[0]
            == 0
        )


def test_partial_checkpoint_resumes_after_reopen(db_path: Path) -> None:
    healthy = _healthy_inputs()
    source = replace(
        healthy,
        gate_deliveries=tuple(
            item for item in healthy.gate_deliveries if item.channel == "github_comment"
        ),
    )
    plan = _plan(source)
    assert len(plan.actions) == 2
    provider = FakeSnapshotProvider({(REPO, PR_NUMBER): _snapshot()})
    with kb.connect_closing(db_path) as conn:
        _seed_gate_bundle(conn, source)
        partial = migration.apply_migration_plan(
            conn,
            plan,
            snapshot_provider=provider,
            confirmation=plan.apply_confirmation,
            operator="test-operator",
            max_actions=1,
            now=NOW + 10,
        )
        assert partial.status == "in_progress"
        assert len(partial.applied_action_ids) == 1
        assert len(partial.pending_action_ids) == 1
        assert partial.checkpoint_count == 1

    with kb.connect_closing(db_path) as conn:
        restored = migration.load_migration_plan(conn, plan.plan_id)
        assert restored is not None
        assert restored == plan
        completed = migration.apply_migration_plan(
            conn,
            restored,
            snapshot_provider=provider,
            confirmation=restored.apply_confirmation,
            operator="test-operator",
            now=NOW + 20,
        )
        assert completed.status == "completed"
        assert len(completed.applied_action_ids) == 1
        assert len(completed.skipped_action_ids) == 1
        assert completed.pending_action_ids == ()
        assert completed.checkpoint_count == 2


def test_automatic_rollback_removes_unchanged_delivery_backfill(
    db_path: Path,
) -> None:
    healthy = _healthy_inputs()
    source = replace(healthy, gate_deliveries=healthy.gate_deliveries[:-1])
    plan = _plan(source)
    provider = FakeSnapshotProvider({(REPO, PR_NUMBER): _snapshot()})
    with kb.connect_closing(db_path) as conn:
        _seed_gate_bundle(conn, source)
        migration.apply_migration_plan(
            conn,
            plan,
            snapshot_provider=provider,
            confirmation=plan.apply_confirmation,
            operator="test-operator",
            now=NOW + 10,
        )
        receipt = migration.rollback_migration_plan(
            conn,
            plan,
            confirmation=plan.rollback_confirmation,
            operator="test-operator",
            now=NOW + 20,
        )
        assert receipt.status == "rolled_back"
        assert receipt.recovery_required_action_ids == ()
        assert len(receipt.rolled_back_action_ids) == 1
        assert receipt.checkpoint_count == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_gate_deliveries WHERE gate_id=? AND channel='slack'",
                (GATE_ID,),
            ).fetchone()[0]
            == 0
        )


def test_stale_gate_apply_suppresses_local_bundle_without_external_side_effects(
    db_path: Path,
) -> None:
    healthy = _healthy_inputs()
    current = _snapshot(head_sha=HEAD_B, observation_id="stale-apply")
    source = replace(
        healthy,
        trusted_pr_reads=(
            reconciliation.TrustedPullRequestRead(
                reconciliation.PullRequestIdentity(REPO, PR_NUMBER),
                "ok",
                current,
            ),
        ),
    )
    plan = _plan(source)
    action = next(item for item in plan.actions if item.kind == "suppress_gate_bundle")
    provider = FakeSnapshotProvider({(REPO, PR_NUMBER): current})
    with kb.connect_closing(db_path) as conn:
        _seed_gate_bundle(conn, source)
        receipt = migration.apply_migration_plan(
            conn,
            plan,
            snapshot_provider=provider,
            confirmation=plan.apply_confirmation,
            operator="test-operator",
            now=NOW + 10,
        )
        assert receipt.status == "completed"
        assert receipt.external_side_effects == "none"
        assert (
            conn.execute(
                "SELECT state FROM human_review_gates WHERE id=?", (GATE_ID,)
            ).fetchone()[0]
            == "superseded"
        )
        assert (
            conn.execute("SELECT status FROM tasks WHERE id=?", (TASK_ID,)).fetchone()[
                0
            ]
            == "archived"
        )
        assert {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT state FROM review_gate_deliveries WHERE gate_id=?",
                (GATE_ID,),
            ).fetchall()
        } == {"superseded"}
        assert {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT state FROM github_human_review_outbox WHERE gate_id=?",
                (GATE_ID,),
            ).fetchall()
        } == {"superseded"}
        assert (
            conn.execute(
                "SELECT state FROM slack_human_review_outbox WHERE gate_id=?",
                (GATE_ID,),
            ).fetchone()[0]
            == "superseded"
        )
        rollback = migration.rollback_migration_plan(
            conn,
            plan,
            confirmation=plan.rollback_confirmation,
            operator="test-operator",
            now=NOW + 20,
        )
        assert rollback.status == "rollback_blocked"
        assert rollback.recovery_required_action_ids == (action.action_id,)
