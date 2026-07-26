import time

import pytest

from hermes_cli import kanban_db as kb


def _grace_loop_pair(conn):
    execution_id = kb.create_task(
        conn,
        title="execution",
        body="GRACE_LOOP_CONTRACT_STAGE: execution",
        assignee="clawops-content",
    )
    review_id = kb.create_task(
        conn,
        title="Grace review",
        body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
        assignee="default",
        parents=(execution_id,),
    )
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', 'chat-1', '2', ?, ?, '{}', 0,
                  'queued', ?, ?, ?, ?)
        """,
        (
            f"gd_{execution_id}",
            (execution_id.replace("t_", "") * 8)[:64],
            f"gri_{execution_id}",
            f"loop:{execution_id}",
            f"loop-session:{execution_id}",
            execution_id,
            review_id,
            now,
            now,
        ),
    )
    conn.commit()
    return execution_id, review_id


def test_review_required_block_is_rejected_when_grace_review_exists(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        with pytest.raises(ValueError, match="would deadlock"):
            kb.block_task(
                conn,
                execution_id,
                reason="review-required: deliverable needs human eyes",
                kind="needs_input",
            )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        violation = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'grace_loop_protocol_violation'",
            (execution_id,),
        ).fetchone()

    assert execution.status == "ready"
    assert review.status == "todo"
    assert violation is not None
    assert review_id in violation["payload"]


def test_genuine_block_is_allowed_when_grace_review_exists(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        assert kb.block_task(
            conn,
            execution_id,
            reason="Missing a KJ product-name decision",
            kind="needs_input",
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)

    assert execution.status == "blocked"
    assert review.status == "todo"


def test_embedded_stage_text_does_not_create_grace_loop_behavior(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        parent = kb.create_task(
            conn,
            title="ordinary parent",
            body="Documentation quotes GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="worker",
        )
        child = kb.create_task(
            conn,
            title="ordinary child",
            body="Evidence quotes GRACE_LOOP_CONTRACT_STAGE: grace_review",
            assignee="worker",
            parents=(parent,),
        )
        assert kb.complete_task(conn, parent, summary="ordinary result")
        claimed_child = kb.claim_task(conn, child)
        assert claimed_child is not None
        assert kb.block_task(
            conn,
            child,
            reason="ordinary dependency",
            kind="dependency",
            expected_run_id=claimed_child.current_run_id,
        )

        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, parent).result is None
        assert (
            conn.execute(
                "SELECT 1 FROM task_events "
                "WHERE task_id = ? AND kind = 'grace_correction_requested'",
                (parent,),
            ).fetchone()
            is None
        )


def test_completion_promotes_dependent_grace_review(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        assert kb.complete_task(
            conn,
            execution_id,
            summary="deliverables verified",
            metadata={"approval_needed": ["public deployment"]},
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)

    assert execution.status == "done"
    assert review.status == "ready"


def test_rejected_grace_review_reopens_execution_for_correction(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        assert kb.complete_task(
            conn,
            execution_id,
            summary="first attempt",
            result="stale result",
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None
        kb.add_comment(
            conn,
            review_id,
            author="default",
            body="Only disable job 4a6d50ce6d18 and preserve read-only checks.",
        )

        assert kb.block_task(
            conn,
            review_id,
            reason="Scheduled distribution job is still enabled",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        correction_comment = conn.execute(
            "SELECT author, body FROM task_comments "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        correction_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'grace_correction_requested' "
            "ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()

        assert execution.status == "ready"
        assert execution.completed_at is None
        assert execution.result is None
        assert review.status == "todo"
        assert review.block_kind == "dependency"
        assert correction_comment["author"] == "Grace review"
        assert "Scheduled distribution job is still enabled" in correction_comment["body"]
        assert "4a6d50ce6d18" not in correction_comment["body"]
        assert correction_event is not None
        assert review_id in correction_event["payload"]
        assert kb.check_respawn_guard(conn, execution_id) is None

        # A dispatcher promotion pass must not immediately re-run the review:
        # its execution parent is open again and must finish correction first.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, review_id).status == "todo"

        assert kb.complete_task(
            conn,
            execution_id,
            summary="correction verified",
        )
        assert kb.get_task(conn, review_id).status == "ready"
        assert kb.check_respawn_guard(conn, execution_id) == "recent_success"
