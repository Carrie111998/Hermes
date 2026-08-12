"""Tests for resolve_review — the DB path backing Discord review buttons."""

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_review_task():
    """Create a task and move it to review via request_review with force=True."""
    conn = kb.connect()
    task_id = kb.create_task(conn, title="review me", assignee="worker")

    assert kb.request_review(
        conn, task_id, summary="All done — please inspect.",
        reviewer="reviewer", force=True,
    )

    event = conn.execute(
        "SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    review_run_id = int(event["run_id"]) if event and event["run_id"] else 0
    assert review_run_id > 0
    return conn, task_id, review_run_id


def test_resolve_review_approve_completes_task(kanban_home):
    """Approve transitions review→done."""
    conn, task_id, run_id = _create_review_task()
    try:
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "review"
        result = kb.resolve_review(conn, task_id, run_id, decision="approve", actor="42")
        assert result == (True, "approve"), f"Expected (True, 'approve') got {result}"
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "done"
        # Idempotent
        assert kb.resolve_review(
            conn, task_id, run_id, decision="approve", actor="43"
        ) == (False, "already_decided")
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_review_decisions WHERE task_id = ?", (task_id,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_resolve_review_reject_records_reason(kanban_home):
    """Reject transitions review→todo/ready with reason."""
    conn, task_id, run_id = _create_review_task()
    try:
        assert kb.resolve_review(
            conn, task_id, run_id, decision="reject", actor="42", reason="missing test coverage"
        ) == (True, "reject")
        row = conn.execute(
            "SELECT decision, actor, reason FROM kanban_review_decisions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert tuple(row) == ("reject", "42", "missing test coverage")
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert status in ("todo", "ready")
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
    finally:
        conn.close()


def test_resolve_review_reject_requires_nonblank_reason(kanban_home):
    """Reject without a reason raises ValueError."""
    conn, task_id, run_id = _create_review_task()
    try:
        with pytest.raises(ValueError, match="reject reason is required"):
            kb.resolve_review(conn, task_id, run_id, decision="reject", actor="42", reason="  \n")
    finally:
        conn.close()


def test_resolve_review_stale_run_fails(kanban_home):
    """A different run_id than the latest review_requested is rejected."""
    conn, task_id, run_id = _create_review_task()
    try:
        assert kb.resolve_review(
            conn, task_id, run_id + 1, decision="approve", actor="42"
        ) == (False, "stale_review")
    finally:
        conn.close()


def test_resolve_review_wrong_status_fails_after_approve(kanban_home):
    """After approve, re-resolution returns already_decided."""
    conn, task_id, run_id = _create_review_task()
    try:
        kb.resolve_review(conn, task_id, run_id, decision="approve", actor="42")
        assert kb.resolve_review(
            conn, task_id, run_id, decision="approve", actor="43"
        ) == (False, "already_decided")
    finally:
        conn.close()


def test_resolve_review_bad_decision_raises(kanban_home):
    """Invalid decision values raise ValueError."""
    conn, task_id, run_id = _create_review_task()
    try:
        with pytest.raises(ValueError, match="decision must be approve or reject"):
            kb.resolve_review(conn, task_id, run_id, decision="maybe", actor="42")
    finally:
        conn.close()


def test_resolve_review_no_actor_raises(kanban_home):
    """Empty actor raises ValueError."""
    conn, task_id, run_id = _create_review_task()
    try:
        with pytest.raises(ValueError, match="actor is required"):
            kb.resolve_review(conn, task_id, run_id, decision="approve", actor="")
    finally:
        conn.close()


def test_resolve_review_event_on_approve(kanban_home):
    """Approve emits a completed event with review metadata."""
    conn, task_id, run_id = _create_review_task()
    try:
        kb.resolve_review(conn, task_id, run_id, decision="approve", actor="42")
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"] or "{}")
        assert payload.get("review_decision") == "approve"
        assert payload.get("actor") == "42"
    finally:
        conn.close()


def test_resolve_review_event_on_reject(kanban_home):
    """Reject emits a review_rejected event with reason."""
    conn, task_id, run_id = _create_review_task()
    try:
        kb.resolve_review(conn, task_id, run_id, decision="reject", actor="42", reason="needs work")
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"] or "{}")
        assert payload.get("review_decision") == "reject"
        assert payload.get("actor") == "42"
        assert payload.get("reason") == "needs work"
    finally:
        conn.close()


def test_resolve_review_task_not_found(kanban_home):
    """Non-existent task returns task_not_found."""
    conn, task_id, run_id = _create_review_task()
    try:
        assert kb.resolve_review(
            conn, "t_nonexistent", run_id, decision="approve", actor="42"
        ) == (False, "task_not_found")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Expiry rollback tests
# ---------------------------------------------------------------------------

REVIEW_EXPIRY_SECONDS = 86400  # 24 hours — matches the production default


def test_release_expired_reviews_rejects_stale_review(kanban_home):
    """A review older than REVIEW_EXPIRY_SECONDS is auto-rejected."""
    conn, task_id, run_id = _create_review_task()
    try:
        # Backdate the review_requested event to simulate expiry
        stale_ts = int(time.time()) - REVIEW_EXPIRY_SECONDS - 3600
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'review_requested'",
            (stale_ts, task_id),
        )
        conn.commit()

        # Call release_expired_reviews
        expired_count = kb.release_expired_reviews(
            conn, expiry_seconds=REVIEW_EXPIRY_SECONDS,
        )
        assert expired_count == 1, f"Expected 1 expired review, got {expired_count}"

        # Verify task was rejected
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()[0]
        assert status in ("todo", "ready"), f"Expected todo/ready, got {status}"

        # Verify decision was recorded
        row = conn.execute(
            "SELECT decision, actor, reason FROM kanban_review_decisions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None, "No review decision recorded"
        assert row["decision"] == "reject"
        assert row["actor"] == "system"
        assert "expired" in row["reason"].lower()

        # Verify review_rejected event emitted
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_rejected' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(event["payload"] or "{}")
        assert payload.get("review_decision") == "reject"
        assert payload.get("actor") == "system"
    finally:
        conn.close()


def test_release_expired_reviews_leaves_fresh_review_alone(kanban_home):
    """A review within the expiry window is NOT auto-rejected."""
    conn, task_id, run_id = _create_review_task()
    try:
        # Review is fresh (just created) — should not expire
        expired_count = kb.release_expired_reviews(
            conn, expiry_seconds=REVIEW_EXPIRY_SECONDS,
        )
        assert expired_count == 0, f"Expected 0 expired, got {expired_count}"

        # Verify task is still in review
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()[0]
        assert status == "review", f"Expected review, got {status}"
    finally:
        conn.close()


def test_release_expired_reviews_already_decided_not_double_counted(kanban_home):
    """An already-decided review is not counted as expired."""
    conn, task_id, run_id = _create_review_task()
    try:
        # Approve the review first
        kb.resolve_review(conn, task_id, run_id, decision="approve", actor="42")

        # Backdate the review_requested event
        stale_ts = int(time.time()) - REVIEW_EXPIRY_SECONDS - 3600
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'review_requested'",
            (stale_ts, task_id),
        )
        conn.commit()

        # Call release_expired_reviews — should find 0 (already done)
        expired_count = kb.release_expired_reviews(
            conn, expiry_seconds=REVIEW_EXPIRY_SECONDS,
        )
        assert expired_count == 0, f"Expected 0, got {expired_count}"
    finally:
        conn.close()


def test_release_expired_reviews_multiple_stale(kanban_home):
    """Multiple expired reviews are all auto-rejected."""
    conn = kb.connect()
    try:
        task_ids = []
        for i in range(3):
            tid = kb.create_task(conn, title=f"review {i}", assignee="worker")
            kb.request_review(conn, tid, summary=f"done {i}", reviewer="reviewer", force=True)
            task_ids.append(tid)

        # Backdate all three
        stale_ts = int(time.time()) - REVIEW_EXPIRY_SECONDS - 3600
        for tid in task_ids:
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'review_requested'",
                (stale_ts, tid),
            )
        conn.commit()

        expired_count = kb.release_expired_reviews(
            conn, expiry_seconds=REVIEW_EXPIRY_SECONDS,
        )
        assert expired_count == 3, f"Expected 3 expired, got {expired_count}"

        for tid in task_ids:
            status = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,),
            ).fetchone()[0]
            assert status in ("todo", "ready"), f"Task {tid} still in {status}"
    finally:
        conn.close()
