"""Typed persistence tests for Epic-member integration intents."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_story_integration import (
    IntegrationIntent,
    IntegrationKey,
    claim_next_intent,
    enqueue_approved_story,
    integration_intent_from_row,
)
from hermes_cli.kanban_product_outcomes import (
    ApprovedCandidate,
    CandidateEligibility,
    PassedTest,
)


SOURCE_SHA = "1" * 40
BASE_SHA = "2" * 40
TARGET_SHA = "3" * 40
CANDIDATE_SHA = "4" * 40


def _claim_board_metadata(repo_root) -> dict[str, object]:
    return {
        "preset": "product",
        "default_workdir": str(repo_root),
        "repository": {
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification_profiles": {
                "story_integration": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
                "epic_release": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
            },
            "ci_observation": {
                "provider": "github_actions",
                "required_workflows": ["CI"],
            },
            "boundary_evidence": {
                "test_globs": ["tests/**"],
                "fixture_globs": ["tests/fixtures/**"],
                "generated_paths": [],
            },
        },
    }


def _insert_claimable_intent(
    conn: sqlite3.Connection,
    *,
    source_sha: str = SOURCE_SHA,
    branch: str = "story/one",
) -> IntegrationKey:
    now = int(time.time())
    epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
    story_id = kb.create_task(
        conn,
        title="Story",
        workflow_template_id="product",
        current_step_key="review",
    )
    kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": branch,
        "test_head_sha": source_sha,
    }
    review_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
        "review_branch": branch,
        "review_base_sha": BASE_SHA,
        "review_head_sha": source_sha,
    }
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
        "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
        (story_id, json.dumps(test_metadata), now - 4, now - 3),
    )
    review_run_id = conn.execute(
        "INSERT INTO task_runs "
        "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
        "VALUES (?, 'review', 'completed', 'advanced', ?, ?, ?)",
        (story_id, json.dumps(review_metadata), now - 2, now - 1),
    ).lastrowid
    with kb.authorized_governance_write(), kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo', assignee=NULL, "
            "running=0, blocked=0, current_run_id=NULL WHERE id=?",
            (epic_id,),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product', "
            "current_step_key='integration_pending', status='review', assignee=NULL, "
            "running=0, blocked=0, current_run_id=NULL, branch_name=? WHERE id=?",
            (branch, story_id),
        )
        conn.execute(
            "INSERT INTO story_integration_intents "
            "(epic_id, story_id, source_sha, source_branch, review_run_id, "
            "review_base_sha, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                epic_id,
                story_id,
                source_sha,
                branch,
                review_run_id,
                BASE_SHA,
                now,
                now,
            ),
        )
    return IntegrationKey(epic_id, story_id, source_sha)


def _intent_values(*, status: str = "prepared") -> tuple[object, ...]:
    return (
        "epic-1",
        "story-1",
        SOURCE_SHA,
        "feature/story-1",
        17,
        BASE_SHA,
        status,
        "owner-1",
        200,
        2,
        TARGET_SHA,
        CANDIDATE_SHA,
        "refs/hermes/candidates/story-1",
        91,
        None,
        100,
        110,
    )


def _insert_intent(conn: sqlite3.Connection, *, status: str = "prepared") -> None:
    conn.execute(
        """
        INSERT INTO story_integration_intents (
            epic_id, story_id, source_sha, source_branch,
            review_run_id, review_base_sha, status, claim_lock,
            claim_expires, attempt_count, target_pre_sha, candidate_sha,
            candidate_ref, verification_event_id, last_failure_code,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _intent_values(status=status),
    )


def test_story_integration_schema_has_exact_columns_primary_key_and_claim_index(
    tmp_path,
):
    with kb.connect(tmp_path / "fresh.db") as conn:
        info = conn.execute(
            "PRAGMA table_info(story_integration_intents)"
        ).fetchall()
        index_columns = conn.execute(
            "PRAGMA index_info(idx_story_integration_intents_claim)"
        ).fetchall()

    assert tuple(row["name"] for row in info) == (
        "epic_id",
        "story_id",
        "source_sha",
        "source_branch",
        "review_run_id",
        "review_base_sha",
        "status",
        "claim_lock",
        "claim_expires",
        "attempt_count",
        "target_pre_sha",
        "candidate_sha",
        "candidate_ref",
        "verification_event_id",
        "last_failure_code",
        "created_at",
        "updated_at",
    )
    assert {row["name"]: row["pk"] for row in info if row["pk"]} == {
        "epic_id": 1,
        "story_id": 2,
        "source_sha": 3,
    }
    assert tuple(row["name"] for row in index_columns) == (
        "status",
        "claim_expires",
        "created_at",
    )


def test_story_integration_schema_round_trips_frozen_intent(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        _insert_intent(conn)
        row = conn.execute("SELECT * FROM story_integration_intents").fetchone()

    intent = integration_intent_from_row(row)

    assert intent == IntegrationIntent(
        key=IntegrationKey("epic-1", "story-1", SOURCE_SHA),
        source_branch="feature/story-1",
        review_run_id=17,
        review_base_sha=BASE_SHA,
        status="prepared",
        claim_lock="owner-1",
        claim_expires=200,
        attempt_count=2,
        target_pre_sha=TARGET_SHA,
        candidate_sha=CANDIDATE_SHA,
        candidate_ref="refs/hermes/candidates/story-1",
        verification_event_id=91,
        last_failure_code=None,
        created_at=100,
        updated_at=110,
    )
    with pytest.raises(FrozenInstanceError):
        intent.status = "integrated"  # type: ignore[misc]


def test_story_integration_schema_enforces_composite_uniqueness(tmp_path):
    with kb.connect(tmp_path / "fresh.db") as conn:
        _insert_intent(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_intent(conn)


@pytest.mark.parametrize("status", ["queued", "done", ""])
def test_story_integration_schema_refuses_illegal_status(tmp_path, status):
    with kb.connect(tmp_path / "fresh.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_intent(conn, status=status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", "abc"),
        ("review_base_sha", "A" * 40),
        ("target_pre_sha", "3" * 39),
        ("candidate_sha", "not-a-sha"),
        ("status", "queued"),
    ],
)
def test_story_integration_parser_refuses_malformed_sha_or_status(field, value):
    row = {
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "source_branch": "feature/story-1",
        "review_run_id": 17,
        "review_base_sha": BASE_SHA,
        "status": "prepared",
        "claim_lock": None,
        "claim_expires": None,
        "attempt_count": 0,
        "target_pre_sha": TARGET_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_ref": None,
        "verification_event_id": None,
        "last_failure_code": None,
        "created_at": 100,
        "updated_at": 100,
    }
    row[field] = value

    with pytest.raises(ValueError):
        integration_intent_from_row(row)


def test_story_integration_parser_keeps_verification_event_audit_only_nullable():
    row = {
        "epic_id": "epic-1",
        "story_id": "story-1",
        "source_sha": SOURCE_SHA,
        "source_branch": "feature/story-1",
        "review_run_id": 17,
        "review_base_sha": BASE_SHA,
        "status": "integrated",
        "claim_lock": None,
        "claim_expires": None,
        "attempt_count": 1,
        "target_pre_sha": TARGET_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_ref": None,
        "verification_event_id": None,
        "last_failure_code": None,
        "created_at": 100,
        "updated_at": 120,
    }

    assert integration_intent_from_row(row).verification_event_id is None


def test_integration_enqueued_transaction_is_idempotent_and_uses_zero_git(
    tmp_path, monkeypatch
):
    branch = "story/one"
    now = int(time.time())
    approved = ApprovedCandidate(
        run_id=0,
        branch=branch,
        base_sha=BASE_SHA,
        source_sha=SOURCE_SHA,
        reviewer_provider="reviewer",
        writer_provider="developer",
    )
    passed = PassedTest(
        run_id=0,
        branch=branch,
        source_sha=SOURCE_SHA,
        tester_provider="tester",
        writer_provider="developer",
    )
    eligibility = CandidateEligibility(source_sha=SOURCE_SHA, non_empty=True)
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "tester": {"agent": "tester", "result": "passed"},
        },
        "test_branch": branch,
        "test_head_sha": SOURCE_SHA,
    }
    review_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
        "review_branch": branch,
        "review_base_sha": BASE_SHA,
        "review_head_sha": SOURCE_SHA,
    }

    monkeypatch.setattr(
        kb,
        "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("enqueue must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("enqueue must not spawn Git"),
    )
    with kb.connect(tmp_path / "enqueue.db") as conn:
        epic_id = kb.create_task(conn, title="Epic", work_item_kind="epic")
        story_id = kb.create_task(
            conn,
            title="Story",
            workflow_template_id="product",
            current_step_key="review",
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        test_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 2, now - 1),
        ).lastrowid
        review_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, metadata, started_at) "
            "VALUES (?, 'review', 'running', ?, ?)",
            (story_id, json.dumps(review_metadata), now),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='running', running=1, assignee='reviewer', "
            "current_run_id=? WHERE id=?",
            (review_run_id, story_id),
        )
        approved = ApprovedCandidate(
            run_id=review_run_id,
            branch=branch,
            base_sha=BASE_SHA,
            source_sha=SOURCE_SHA,
            reviewer_provider="reviewer",
            writer_provider="developer",
        )
        passed = PassedTest(
            run_id=test_run_id,
            branch=branch,
            source_sha=SOURCE_SHA,
            tester_provider="tester",
            writer_provider="developer",
        )

        first = enqueue_approved_story(
            conn,
            epic_id=epic_id,
            story_id=story_id,
            approved=approved,
            passed=passed,
            eligibility=eligibility,
            expected_run_id=review_run_id,
            summary="approved",
            metadata=review_metadata,
        )
        replay = enqueue_approved_story(
            conn,
            epic_id=epic_id,
            story_id=story_id,
            approved=approved,
            passed=passed,
            eligibility=eligibility,
            expected_run_id=review_run_id,
        )
        stale_test_metadata = dict(test_metadata)
        stale_test_metadata["test_head_sha"] = "9" * 40
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(stale_test_metadata), test_run_id),
        )
        with pytest.raises(ValueError, match="stale"):
            enqueue_approved_story(
                conn,
                epic_id=epic_id,
                story_id=story_id,
                approved=approved,
                passed=passed,
                eligibility=eligibility,
                expected_run_id=review_run_id,
            )
        story = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, assignee, "
            "current_run_id FROM tasks WHERE id=?",
            (story_id,),
        ).fetchone()
        epic = conn.execute(
            "SELECT workflow_template_id, current_step_key, status, assignee "
            "FROM tasks WHERE id=?",
            (epic_id,),
        ).fetchone()
        events = [event for event in kb.list_events(conn, story_id)
                  if event.kind == "story_integration_enqueued"]

    assert replay == first
    assert first.status == "pending"
    assert tuple(story) == ("product", "integration_pending", "review", None, None)
    assert tuple(epic) == ("product_epic", "collecting_members", "todo", None)
    assert len(events) == 1
    assert events[0].payload == {
        "epic_id": epic_id,
        "story_id": story_id,
        "source_sha": SOURCE_SHA,
        "review_run_id": review_run_id,
    }


def test_claim_next_intent_has_one_winner_across_two_connections(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "claim.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        expected_key = _insert_claimable_intent(conn)

    repository_calls = []
    barrier = threading.Barrier(2)

    def repository_check(contract, approved, passed):
        repository_calls.append((contract, approved, passed))
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    def claim(owner: str):
        with kb.connect(db_path) as conn:
            barrier.wait(timeout=5)
            return claim_next_intent(
                conn,
                owner,
                60,
                repository_check=repository_check,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("owner-a", "owner-b")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].key == expected_key
    assert winners[0].status == "running"
    assert winners[0].attempt_count == 1
    assert len(repository_calls) == 1
    with kb.connect(db_path) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM story_integration_intents WHERE status='running'"
        ).fetchone()[0]
    assert running == 1


def test_claim_next_intent_reclaims_expired_intent_before_new_work(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "reclaim.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        _insert_claimable_intent(conn, branch="story/first")
        _insert_claimable_intent(conn, source_sha="5" * 40, branch="story/second")

    repository_calls = []

    def repository_check(contract, approved, passed):
        repository_calls.append(approved.source_sha)
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    monkeypatch.setattr(time, "time", lambda: 100)
    with kb.connect(db_path) as conn:
        first = claim_next_intent(
            conn,
            "owner-a",
            60,
            repository_check=repository_check,
        )
        assert claim_next_intent(
            conn,
            "owner-b",
            60,
            repository_check=repository_check,
        ) is None

    monkeypatch.setattr(time, "time", lambda: 161)
    with kb.connect(db_path) as conn:
        reclaimed = claim_next_intent(
            conn,
            "owner-b",
            60,
            repository_check=repository_check,
        )

    assert first is not None and reclaimed is not None
    assert first.key == reclaimed.key
    assert first.claim_lock != reclaimed.claim_lock
    assert reclaimed.attempt_count == 2
    assert repository_calls == [first.key.source_sha, first.key.source_sha]


def test_claim_next_intent_fails_closed_on_running_lease_without_expiry(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "missing-expiry.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        first_key = _insert_claimable_intent(conn, branch="story/first")
        _insert_claimable_intent(conn, source_sha="5" * 40, branch="story/second")
        conn.execute(
            "UPDATE story_integration_intents SET status='running', "
            "claim_lock='owner:existing', claim_expires=NULL "
            "WHERE epic_id=? AND story_id=? AND source_sha=?",
            (first_key.epic_id, first_key.story_id, first_key.source_sha),
        )

    repository_calls = []
    with kb.connect(db_path) as conn:
        claimed = claim_next_intent(
            conn,
            "owner-new",
            60,
            repository_check=lambda *_args: repository_calls.append(True),
        )
        running = conn.execute(
            "SELECT COUNT(*) FROM story_integration_intents WHERE status='running'"
        ).fetchone()[0]

    assert claimed is None
    assert repository_calls == []
    assert running == 1


@pytest.mark.parametrize(
    "stale_case",
    [
        "membership",
        "test",
        "review",
        "provider",
        "test_provider",
        "sha",
        "source_branch",
        "directive",
        "phase",
        "contract",
    ],
)
def test_claim_next_intent_refuses_stale_authority_before_repository_access(
    tmp_path, monkeypatch, stale_case
):
    db_path = tmp_path / f"stale-{stale_case}.db"
    board_metadata = _claim_board_metadata(tmp_path)
    monkeypatch.setattr(kb, "product_board_metadata", lambda _board=None: board_metadata)
    with kb.connect(db_path) as conn:
        key = _insert_claimable_intent(conn)
        if stale_case == "membership":
            conn.execute(
                "DELETE FROM epic_memberships WHERE epic_id=? AND task_id=?",
                (key.epic_id, key.story_id),
            )
        elif stale_case in {"test", "review", "provider", "test_provider", "sha"}:
            phase = "test" if stale_case in {"test", "test_provider"} else "review"
            row = conn.execute(
                "SELECT id, metadata FROM task_runs WHERE task_id=? AND step_key=? "
                "ORDER BY id DESC LIMIT 1",
                (key.story_id, phase),
            ).fetchone()
            metadata = json.loads(row["metadata"])
            if stale_case in {"test", "review"}:
                metadata["workflow_outcome"] = {
                    "verdict": "changes_requested",
                    "target_step": "development",
                    "findings": ["later evidence rejected the candidate"],
                }
            elif stale_case == "provider":
                metadata["ai_provenance"]["reviewer"]["agent"] = "developer"
            elif stale_case == "test_provider":
                metadata["ai_provenance"]["tester"]["agent"] = "developer"
            else:
                metadata["review_head_sha"] = "9" * 40
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), row["id"]),
            )
        elif stale_case == "source_branch":
            conn.execute(
                "UPDATE tasks SET branch_name='story/moved' WHERE id=?",
                (key.story_id,),
            )
        elif stale_case == "directive":
            kb.create_rework_directive(
                conn,
                key.story_id,
                origin_kind="review",
                origin_phase="review",
                target_phase="development",
                rejected_branch="story/one",
                rejected_sha=SOURCE_SHA,
                findings=["rework is active"],
            )
        elif stale_case == "phase":
            conn.execute(
                "UPDATE tasks SET current_step_key='review' WHERE id=?",
                (key.story_id,),
            )
        else:
            repository = board_metadata["repository"]
            assert isinstance(repository, dict)
            repository["verification_profiles"].pop("story_integration")

    repository_calls = []

    def repository_check(contract, approved, passed):
        repository_calls.append((contract, approved, passed))
        return CandidateEligibility(source_sha=approved.source_sha, non_empty=True)

    with kb.connect(db_path) as conn:
        claimed = claim_next_intent(
            conn,
            "owner",
            60,
            repository_check=repository_check,
        )

    assert claimed is None
    assert repository_calls == []
