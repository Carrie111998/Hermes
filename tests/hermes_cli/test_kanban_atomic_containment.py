"""Atomic containment and durable dispatch-hold regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kanban.db"


@pytest.fixture
def conn(db_path: Path):
    db = kb.connect(db_path)
    try:
        yield db
    finally:
        db.close()


def _claimed(conn, *, title: str = "worker") -> tuple[str, int]:
    task_id = kb.create_task(conn, title=title, assignee="developer")
    claimed = kb.claim_task(conn, task_id, claimer="host:worker")
    assert claimed is not None and claimed.current_run_id is not None
    return task_id, claimed.current_run_id


def test_containment_lands_block_comment_link_and_hold_atomically(
    conn, db_path: Path, monkeypatch,
) -> None:
    parent_id = kb.create_task(conn, title="remediation", assignee="developer")
    task_id, run_id = _claimed(conn)
    observations: list[dict] = []

    def terminate(_pid, _lock, **_kwargs):
        competing = kb.connect(db_path)
        try:
            task = kb.get_task(competing, task_id)
            observations.append({
                "status": task.status,
                "hold": task.dispatch_hold_reason,
                "comments": len(kb.list_comments(competing, task_id)),
                "parents": kb.parent_ids(competing, task_id),
            })
        finally:
            competing.close()
        return {"terminated": True, "still_alive": False}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", terminate)
    assert kb.contain_task(
        conn,
        task_id,
        reason="unsafe worker output",
        author="operator",
        expected_run_id=run_id,
        parent_id=parent_id,
    )

    assert observations == [{
        "status": "running", "hold": None, "comments": 0, "parents": [],
    }]
    competing = kb.connect(db_path)
    try:
        task = kb.get_task(competing, task_id)
        assert task.status == "blocked"
        assert task.dispatch_hold_reason == "unsafe worker output"
        assert kb.parent_ids(competing, task_id) == [parent_id]
        assert [c.body for c in kb.list_comments(competing, task_id)] == [
            "unsafe worker output"
        ]
        assert kb.claim_task(competing, task_id) is None
    finally:
        competing.close()


def test_link_refuses_unfinished_parent_behind_live_claim(conn) -> None:
    parent_id = kb.create_task(conn, title="unfinished", assignee="planner")
    child_id, _run_id = _claimed(conn, title="live child")

    with pytest.raises(ValueError, match="running child"):
        kb.link_tasks(conn, parent_id, child_id)

    assert kb.parent_ids(conn, child_id) == []
    assert kb.get_task(conn, child_id).status == "running"


def test_surviving_worker_stays_owned_and_held(conn, monkeypatch) -> None:
    task_id, run_id = _claimed(conn)
    before = kb.get_task(conn, task_id)

    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *_args, **_kwargs: {
            "host_local": True,
            "termination_attempted": True,
            "terminated": False,
        },
    )
    assert kb.contain_task(
        conn,
        task_id,
        reason="termination did not stop worker",
        author="operator",
        expected_run_id=run_id,
    )

    after = kb.get_task(conn, task_id)
    assert after.status == "running"
    assert after.current_run_id == run_id
    assert after.claim_lock == before.claim_lock
    assert after.dispatch_hold_reason == "termination did not stop worker"
    assert kb.claim_task(conn, task_id) is None


def test_completion_can_hold_children_before_they_promote(conn) -> None:
    parent_id = kb.create_task(conn, title="parent", assignee="developer")
    child_id = kb.create_task(
        conn, title="child", assignee="developer", parents=[parent_id],
    )
    assert kb.get_task(conn, child_id).status == "todo"

    assert kb.complete_task(
        conn,
        parent_id,
        hold_children_reason="awaiting immutable QA artifact",
        hold_author="operator",
    )

    child = kb.get_task(conn, child_id)
    assert child.status == "todo"
    assert child.dispatch_hold_reason == "awaiting immutable QA artifact"
    assert kb.claim_task(conn, child_id) is None


def test_hold_release_is_explicit_audited_and_recomputes_ready(conn) -> None:
    task_id = kb.create_task(conn, title="held", assignee="developer")
    assert kb.set_dispatch_hold(
        conn, task_id, reason="preflight mismatch", author="dispatcher"
    )
    assert kb.get_task(conn, task_id).dispatch_hold_reason == "preflight mismatch"
    assert kb.claim_task(conn, task_id) is None

    assert kb.release_dispatch_hold(conn, task_id, author="operator")

    task = kb.get_task(conn, task_id)
    assert task.dispatch_hold_reason is None
    assert task.status == "ready"
    events = kb.list_events(conn, task_id)
    assert [event.kind for event in events][-1] == "dispatch_hold_released"
    assert events[-1].payload["prior_reason"] == "preflight mismatch"


def test_stale_run_cannot_contain_successor(conn, monkeypatch) -> None:
    task_id, stale_run_id = _claimed(conn)
    assert kb.reclaim_task(conn, task_id, signal_fn=lambda *_a, **_k: {})
    successor = kb.claim_task(conn, task_id, claimer="host:successor")
    assert successor is not None

    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *_args, **_kwargs: pytest.fail("stale containment attempted termination"),
    )
    assert not kb.contain_task(
        conn,
        task_id,
        reason="stale operator request",
        author="operator",
        expected_run_id=stale_run_id,
    )

    current = kb.get_task(conn, task_id)
    assert current.status == "running"
    assert current.current_run_id == successor.current_run_id
    assert current.dispatch_hold_reason is None
