"""Tests for resolve_review — the DB path backing Discord review buttons."""

import json
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