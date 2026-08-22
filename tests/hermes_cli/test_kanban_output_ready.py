from __future__ import annotations

from hermes_cli import kanban_db as kb


def _task(tmp_path, *, status="running"):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = kb.connect(db_path)
    task_id = kb.create_task(
        conn,
        title="shadow output",
        assignee="codex",
        initial_status=status,
    )
    return conn, task_id


def test_output_ready_is_durable_before_review(tmp_path):
    conn, task_id = _task(tmp_path)

    assert kb.publish_task_output(
        conn,
        task_id,
        summary="Artifact is ready for the user.",
        metadata={"proof": {"checks": ["unit"]}},
    )
    assert kb.get_task(conn, task_id).status == "output_ready"
    output_run = kb.list_runs(conn, task_id)[-1]
    assert output_run.outcome == "output_ready"
    assert output_run.summary == "Artifact is ready for the user."
    assert kb.list_events(conn, task_id)[-1].kind == "output_ready"

    assert kb.request_review(
        conn,
        task_id,
        summary="Artifact is ready for the user.",
        metadata={"proof": {"checks": ["unit"]}},
        reviewer="codex-reviewer",
    )
    task = kb.get_task(conn, task_id)
    assert task.status == "review"
    assert task.assignee == "codex-reviewer"
    conn.close()


def test_interactive_working_task_is_not_a_dispatcher_claim(tmp_path):
    conn, task_id = _task(tmp_path, status="working")

    task = kb.get_task(conn, task_id)
    assert task.status == "working"
    assert task.claim_lock is None
    assert kb.claim_task(conn, task_id) is None
    assert kb.publish_task_output(conn, task_id, summary="Foreground output")
    assert kb.get_task(conn, task_id).status == "output_ready"
    conn.close()


def test_basic_output_can_complete_from_output_ready(tmp_path, monkeypatch):
    conn, task_id = _task(tmp_path)
    assert kb.publish_task_output(conn, task_id, summary="Ready", metadata={"proof": {"checks": 1}})
    monkeypatch.setattr(kb, "_kanban_observer_consumed", lambda _event: False)

    assert kb.complete_task(conn, task_id, summary="Ready", result="Ready")
    assert kb.get_task(conn, task_id).status == "done"
    kinds = [event.kind for event in kb.list_events(conn, task_id)]
    assert kinds.index("output_ready") < kinds.index("completed")
    conn.close()


def test_live_claim_requires_run_ownership_to_publish(tmp_path):
    conn, task_id = _task(tmp_path, status="blocked")
    assert kb.promote_task(conn, task_id, actor="test")[0]
    claimed = kb.claim_task(conn, task_id, claimer="worker")
    assert claimed is not None and claimed.current_run_id is not None

    assert kb.publish_task_output(
        conn,
        task_id,
        summary="Ready",
        with_reason=True,
    )[0] is False
    assert kb.publish_task_output(
        conn,
        task_id,
        summary="Ready",
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "output_ready"
    conn.close()
