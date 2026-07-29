from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_codex_bridge import (
    CodexCommentForwarder,
    CodexHostCommandForwarder,
    begin_comment_delivery,
    finish_execution,
    prepare_execution,
    record_active_runtime,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.init_db()
    with kb.connect_closing(db_path=db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="Implement the accepted card",
            assignee="forge",
            initial_status="blocked",
        )
        kb.add_comment(conn, task_id, "hermes", "Acceptance criteria are on-card.")
        assert kb.unblock_task(conn, task_id)
        task = kb.claim_task(conn, task_id, claimer="test-worker")
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
    yield db_path, task_id, run_id


class FakeSession:
    def __init__(self, thread_id="thread-1", turn_id="turn-1"):
        self.thread_id = thread_id
        self.active_turn_id = turn_id
        self.steered = []
        self.existing_client_ids = set()
        self.interrupted = False

    def steer_turn(self, text, **kwargs):
        self.steered.append((text, kwargs))
        return {}

    def has_user_message(self, client_id):
        return client_id in self.existing_client_ids

    def request_interrupt(self):
        self.interrupted = True


def test_prepare_rejects_ready_task_or_stale_run(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        assert kb.reclaim_task(conn, task_id, reason="test stale worker")
        with kb.write_txn(conn):
            with pytest.raises(RuntimeError, match="current live claimed run"):
                prepare_execution(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    initial_comment_id=1,
                )


def test_host_command_forwarder_executes_once_and_returns_result(board):
    db_path, task_id, run_id = board
    calls = []
    forwarder = CodexHostCommandForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=run_id,
        executor=lambda tool, arguments: (
            calls.append((tool, arguments))
            or {"ok": True, "exit_code": 0}
        ),
        poll_interval=0.01,
    )
    forwarder.start()
    try:
        from hermes_cli.kanban_codex_bridge import request_host_broker_command

        result = request_host_broker_command(
            db_path=db_path,
            task_id=task_id,
            run_id=run_id,
            tool="git",
            arguments=["add", "--", "README.md"],
            timeout=2,
        )
    finally:
        forwarder.stop()

    assert result == {"ok": True, "exit_code": 0}
    assert calls == [("git", ["add", "--", "README.md"])]


def test_comments_after_atomic_snapshot_are_steered_once_and_self_comments_ignored(
    board,
):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepared = prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
        assert prepared.last_comment_id == 1
        comment_id = kb.add_comment(
            conn, task_id, "gavin", "Please include the mobile path."
        )
        self_comment_id = kb.add_comment(
            conn, task_id, "forge", "CODEX CHECKPOINT: tests are running."
        )

    session = FakeSession()
    forwarder = CodexCommentForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=run_id,
        session=session,
        ignored_authors={"forge"},
    )
    assert forwarder.poll_once() == 1
    assert forwarder.poll_once() == 0
    assert len(session.steered) == 1
    text, kwargs = session.steered[0]
    assert "gavin" in text
    assert "mobile path" in text
    assert kwargs["expected_turn_id"] == "turn-1"
    assert kwargs["client_user_message_id"].endswith(f"-comment-{comment_id}")
    with kb.connect_closing(db_path=db_path) as conn:
        rows = conn.execute(
            """
            SELECT comment_id, state
              FROM task_executor_comment_deliveries
             WHERE task_id = ?
             ORDER BY comment_id
            """,
            (task_id,),
        ).fetchall()
    assert [(row["comment_id"], row["state"]) for row in rows] == [
        (comment_id, "accepted"),
        (self_comment_id, "ignored"),
    ]


def test_ambiguous_steer_is_reconciled_from_thread_before_resend(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
        comment_id = kb.add_comment(conn, task_id, "gavin", "Keep this exactly once.")
        client_id = f"hermes-kanban-{task_id}-comment-{comment_id}"
        now = 1
        conn.execute(
            """
            INSERT INTO task_executor_comment_deliveries (
                task_id, comment_id, first_run_id, last_run_id,
                client_message_id, state, attempts, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 1, 'timeout', ?, ?)
            """,
            (task_id, comment_id, run_id, run_id, client_id, now, now),
        )

    session = FakeSession()
    session.existing_client_ids.add(client_id)
    forwarder = CodexCommentForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=run_id,
        session=session,
    )
    assert forwarder.poll_once() == 1
    assert session.steered == []


def test_retry_snapshot_includes_down_time_comments_without_steering_them_twice(
    board,
):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
            record_active_runtime(
                conn,
                task_id=task_id,
                run_id=run_id,
                thread_id="thread-original",
                turn_id="turn-original",
            )
        down_time_comment_id = kb.add_comment(
            conn, task_id, "gavin", "This arrived while the worker was down."
        )
        with kb.write_txn(conn):
            delivery = begin_comment_delivery(
                conn,
                task_id=task_id,
                run_id=run_id,
                comment_id=down_time_comment_id,
            )
        assert delivery.state == "pending"
        assert kb.reclaim_task(conn, task_id, reason="worker crashed")
        retry_task = kb.claim_task(conn, task_id, claimer="retry-worker")
        assert retry_task is not None and retry_task.current_run_id is not None
        retry_run_id = retry_task.current_run_id
        with kb.write_txn(conn):
            retry = prepare_execution(
                conn,
                task_id=task_id,
                run_id=retry_run_id,
                initial_comment_id=2,
            )

    assert retry.resume_thread_id == "thread-original"
    assert retry.last_comment_id == down_time_comment_id
    session = FakeSession(thread_id="thread-original", turn_id="turn-retry")
    forwarder = CodexCommentForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=retry_run_id,
        session=session,
    )
    # The resumed run's fresh atomic prompt already contains the comment.
    assert forwarder.poll_once() == 0
    assert session.steered == []
    with kb.connect_closing(db_path=db_path) as conn:
        state = conn.execute(
            """
            SELECT state
              FROM task_executor_comment_deliveries
             WHERE task_id = ? AND comment_id = ?
            """,
            (task_id, down_time_comment_id),
        ).fetchone()["state"]
        assert state == "included"
        later_comment_id = kb.add_comment(
            conn, task_id, "gavin", "This arrived after the retry snapshot."
        )

    assert forwarder.poll_once() == 1
    assert "after the retry snapshot" in session.steered[0][0]
    assert session.steered[0][1]["client_user_message_id"].endswith(
        f"-comment-{later_comment_id}"
    )


def test_completed_run_is_not_resumed_after_crash_before_bridge_finalizer(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
            record_active_runtime(
                conn,
                task_id=task_id,
                run_id=run_id,
                thread_id="thread-closed",
                turn_id=None,
            )
        assert kb.complete_task(conn, task_id, result="delivered")
        # Simulate dashboard done -> ready after the worker died before
        # finish_execution could mark its bridge row closed.
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
            (task_id,),
        )
        fresh_task = kb.claim_task(conn, task_id, claimer="new-outcome")
        assert fresh_task is not None and fresh_task.current_run_id is not None
        with kb.write_txn(conn):
            fresh = prepare_execution(
                conn,
                task_id=task_id,
                run_id=fresh_task.current_run_id,
                initial_comment_id=1,
            )

    assert fresh.resume_thread_id is None


def test_operator_resume_after_capability_block_reuses_thread(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
            record_active_runtime(
                conn,
                task_id=task_id,
                run_id=run_id,
                thread_id="thread-capability",
                turn_id="turn-capability",
            )
        assert kb.block_task(
            conn,
            task_id,
            reason="task-scoped commit capability unavailable",
            kind="capability",
        )
        with kb.write_txn(conn):
            finish_execution(conn, task_id=task_id, run_id=run_id)
        state = conn.execute(
            "SELECT state FROM task_executor_sessions "
            "WHERE task_id = ? AND run_id = ?",
            (task_id, run_id),
        ).fetchone()["state"]
        assert state == "attention"
        # Rows finalized by the pre-fix bridge used ``closed`` for a blocked
        # run. They remain resumable because the durable run outcome, not that
        # historical projection, is authoritative.
        conn.execute(
            "UPDATE task_executor_sessions SET state = 'closed' "
            "WHERE task_id = ? AND run_id = ?",
            (task_id, run_id),
        )
        conn.commit()

        assert kb.unblock_task(conn, task_id)
        retry_task = kb.claim_task(conn, task_id, claimer="operator-resume")
        assert retry_task is not None and retry_task.current_run_id is not None
        with kb.write_txn(conn):
            retry = prepare_execution(
                conn,
                task_id=task_id,
                run_id=retry_task.current_run_id,
                initial_comment_id=1,
            )

    assert retry.resume_thread_id == "thread-capability"


def test_terminal_card_with_late_comment_records_attention_instead_of_closed(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
        kb.add_comment(conn, task_id, "gavin", "Arrived at completion boundary.")
        assert kb.complete_task(conn, task_id, result="delivered")
        with kb.write_txn(conn):
            finish_execution(conn, task_id=task_id, run_id=run_id)
        state = conn.execute(
            """
            SELECT state FROM task_executor_sessions
             WHERE task_id = ? AND run_id = ?
            """,
            (task_id, run_id),
        ).fetchone()["state"]
    assert state == "attention"


def test_successful_terminal_transition_stops_comment_bridge_cleanly(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
            record_active_runtime(
                conn,
                task_id=task_id,
                run_id=run_id,
                thread_id="thread-1",
                turn_id="turn-1",
            )
        assert kb.complete_task(conn, task_id, result="delivered")

    forwarder = CodexCommentForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=run_id,
        session=FakeSession(),
    )
    assert forwarder.poll_once() == 0
    assert forwarder._stop.is_set()
    forwarder.check_health()


def test_dependency_wait_stops_comment_bridge_after_run_lease_is_released(board):
    db_path, task_id, run_id = board
    with kb.connect_closing(db_path=db_path) as conn:
        with kb.write_txn(conn):
            prepare_execution(
                conn,
                task_id=task_id,
                run_id=run_id,
                initial_comment_id=1,
            )
            record_active_runtime(
                conn,
                task_id=task_id,
                run_id=run_id,
                thread_id="thread-1",
                turn_id="turn-1",
            )
        assert kb.block_task(
            conn,
            task_id,
            reason="waiting on fixture",
            kind="dependency",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, task_id).status == "todo"

    forwarder = CodexCommentForwarder(
        db_path=db_path,
        task_id=task_id,
        run_id=run_id,
        session=FakeSession(),
    )
    assert forwarder.poll_once() == 0
    assert forwarder._stop.is_set()
    forwarder.check_health()
