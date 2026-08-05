"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                author="me",
            )


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_children_inherit_dir_workspace(kanban_home):
    """Fan-out children inherit the root's dir workspace, not scratch."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        for cid in child_ids:
            t = kb.get_task(conn, cid)
            assert t.workspace_kind == "dir"
            assert t.workspace_path == proj


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}], author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "inherit"},
            ],
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        inh = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert inh.workspace_path == proj


def test_recursive_children_are_triage_and_receipt_bound_to_root_digest(kanban_home):
    digest = "a" * 64
    root_body = f"Frozen artifact: {digest}\nR=1;T=400;"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="recursive root",
            body=root_body,
            triage=True,
            recursion_enabled=True,
            recursion_trigger_chars=400,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            root_frozen_plan_digest=digest,
            children=[
                {
                    "title": "recursive child",
                    "body": f"Frozen artifact: {digest}\nchild body",
                    "assignee": "engineer",
                    "depth": 2,
                    "plan_item_index": 0,
                },
            ],
            author="decomposer",
        )
        assert child_ids
        child = kb.get_task(conn, child_ids[0])
        assert child is not None
        assert child.status == "triage"
        assert kb.claim_task(conn, child.id) is None
        created = [event for event in kb.list_events(conn, child.id) if event.kind == "created"]
        decomposed = [event for event in kb.list_events(conn, tid) if event.kind == "decomposed"]

    assert created[-1].payload == {
        "by": "decomposer",
        "from_decompose_of": tid,
        "depth": 2,
        "plan_item_index": 0,
        "root_frozen_plan_digest": digest,
    }
    assert decomposed[-1].payload["root_frozen_plan_digest"] == digest


def test_recursive_digest_mismatch_rolls_back_all_inserts(kanban_home):
    root_digest = "b" * 64
    wrong_digest = "c" * 64
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="recursive root",
            body=f"Frozen artifact: {root_digest}\nR=1;T=400;",
            triage=True,
            recursion_enabled=True,
            recursion_trigger_chars=400,
        )
        with pytest.raises(ValueError, match="digest mismatch"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                root_frozen_plan_digest=root_digest,
                children=[
                    {
                        "title": "wrong binding",
                        "body": f"Frozen artifact: {wrong_digest}",
                        "depth": 2,
                        "plan_item_index": 0,
                    },
                ],
                author="decomposer",
            )
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id != ?", (tid,)).fetchone()[0] == 0


def test_recursive_decomposition_skips_post_commit_ready_update(kanban_home, monkeypatch):
    digest = "d" * 64
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="recursive root",
            body=f"Frozen artifact: {digest}\nR=1;T=400;",
            triage=True,
        )

        def fail_recompute(_conn):
            raise AssertionError("recursive decomposition must not recompute ready after commit")

        monkeypatch.setattr(kb, "recompute_ready", fail_recompute)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            root_frozen_plan_digest=digest,
            children=[
                {
                    "title": "recursive child",
                    "body": f"Frozen artifact: {digest}",
                    "depth": 2,
                    "plan_item_index": 0,
                },
            ],
            author="decomposer",
        )
        assert child_ids


def test_recursive_child_is_not_claimable_until_commit_and_stays_triage(
    kanban_home,
    monkeypatch,
):
    digest = "1" * 64
    known_child_id = "t_recursive_race"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="recursive root",
            body=f"Frozen artifact: {digest}\nR=1;T=400;",
            triage=True,
        )

    commit_entered = threading.Event()
    release_commit = threading.Event()
    original_boundary = kb._execute_boundary_with_retry

    def hold_recursive_commit(conn, sql):
        if sql == "COMMIT" and threading.current_thread().name == "decomposer":
            commit_entered.set()
            assert release_commit.wait(timeout=5)
        return original_boundary(conn, sql)

    monkeypatch.setattr(kb, "_new_task_id", lambda: known_child_id)
    monkeypatch.setattr(kb, "_execute_boundary_with_retry", hold_recursive_commit)
    result: dict[str, object] = {}

    def decompose():
        with kb.connect() as conn:
            result["ids"] = kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                root_frozen_plan_digest=digest,
                children=[
                    {
                        "title": "recursive child",
                        "body": f"Frozen artifact: {digest}",
                        "depth": 2,
                        "plan_item_index": 0,
                    },
                ],
                author="decomposer",
            )

    worker = threading.Thread(target=decompose, name="decomposer")
    worker.start()
    assert commit_entered.wait(timeout=5)

    claim_result: dict[str, object] = {}

    def claim():
        with kb.connect() as conn:
            claim_result["task"] = kb.claim_task(conn, known_child_id)

    claimer = threading.Thread(target=claim, name="claimer")
    claimer.start()
    # The claimer is blocked behind the uncommitted write, never observing a
    # partially inserted ready child. Releasing the commit lets it read the
    # final triage status and complete its CAS attempt.
    release_commit.set()
    worker.join(timeout=5)
    claimer.join(timeout=5)
    assert not worker.is_alive()
    assert not claimer.is_alive()
    assert result["ids"] == [known_child_id]
    assert claim_result["task"] is None
