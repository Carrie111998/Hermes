"""Typed persistence tests for Epic-member integration intents."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_story_integration import (
    IntegrationIntent,
    IntegrationKey,
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
