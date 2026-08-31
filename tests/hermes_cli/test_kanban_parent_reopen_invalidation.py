"""Regressions for domain-layer descendant invalidation on ancestor reopen.

``kanban_db.invalidate_descendants_for_parent_reopen`` is the single
implementation of "a done ancestor was reopened, retract everything that
assumed its result" (M3). These tests pin:

* done descendants are demoted to ``todo`` with a ``descendant_invalidated``
  event AND a comment naming the ancestor (non-silent),
* running descendants have their audit trail committed BEFORE their worker
  is terminated, and the kill routes through ``_terminate_reclaimed_worker``
  (the same helper the reclaim paths use),
* ``consecutive_failures`` resets to 0 (deliberate operator action —
  opposite of the review-loop rule pinned in M2), and
* the dashboard ``_set_status_direct`` reopen path and the DB function
  produce identical descendant outcomes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_containment as kc


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _done_parent_with_done_child(conn):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id)
    child_id = kb.create_task(
        conn, title="child", assignee="builder", parents=[parent_id],
    )
    assert kb.complete_task(conn, child_id)
    return parent_id, child_id


def _reopen_parent_directly(conn, parent_id: str) -> None:
    """Minimal stand-in for a reopen surface: flip done -> todo."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
            (parent_id,),
        )


def test_reopen_demotes_done_descendants_with_events_and_comments(conn):
    parent_id, child_id = _done_parent_with_done_child(conn)
    grandchild_id = kb.create_task(
        conn, title="grandchild", assignee="writer", parents=[child_id],
    )
    assert kb.complete_task(conn, grandchild_id)

    _reopen_parent_directly(conn, parent_id)
    result = kb.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    demoted = {entry["id"]: entry for entry in result["invalidated"]}
    assert set(demoted) == {child_id, grandchild_id}
    for tid in (child_id, grandchild_id):
        assert demoted[tid]["prior_status"] == "done"
        assert demoted[tid]["new_status"] == "todo"
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "todo"
        assert task.completed_at is None

        events = kb.list_events(conn, tid)
        inval = [e for e in events if e.kind == "descendant_invalidated"]
        assert len(inval) == 1
        payload = inval[0].payload
        assert payload["ancestor"] == parent_id
        assert payload["prior_status"] == "done"
        assert payload["new_status"] == "todo"

        comments = kb.list_comments(conn, tid)
        assert any(
            parent_id in c.body and c.author == "operator" for c in comments
        ), f"no invalidation comment naming {parent_id} on {tid}"

    assert result["terminations"] == []


def test_running_descendant_event_precedes_termination_via_reclaim_helper(
    conn, tmp_path, monkeypatch,
):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id)
    child_id = kb.create_task(
        conn, title="running child", assignee="builder", parents=[parent_id],
    )
    claimed = kb.claim_task(conn, child_id)
    assert claimed is not None and claimed.status == "running"
    kb._set_worker_pid(conn, child_id, 424242)

    kills: list[tuple] = []

    def fake_terminate(pid, claim_lock, **kwargs):
        # The audit trail must already be durable when the kill fires:
        # standalone calls commit before terminating.
        side = kb.connect(tmp_path / "kanban.db")
        try:
            kinds = [e.kind for e in kb.list_events(side, child_id)]
        finally:
            side.close()
        assert "descendant_invalidated" in kinds
        kills.append((pid, claim_lock))
        return {"terminated": True}

    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", fake_terminate)

    _reopen_parent_directly(conn, parent_id)
    result = kb.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    assert kills and kills[0][0] == 424242
    assert result["terminations"] == kills
    child = kb.get_task(conn, child_id)
    assert child is not None
    assert child.status == "todo"
    assert child.current_run_id is None
    run = kb.latest_run(conn, child_id)
    assert run is not None and run.outcome == "reclaimed"


def test_contained_descendants_are_all_reserved_and_certified_before_invalidation(
    conn, monkeypatch,
):
    parent_id = kb.create_task(conn, title="ancestor", assignee="planner")
    assert kb.complete_task(conn, parent_id)
    children = []
    for index in range(2):
        child_id = kb.create_task(
            conn,
            title=f"contained child {index}",
            assignee="builder",
            parents=[parent_id],
        )
        claimed = kb.claim_task(conn, child_id, claimer=f"host:owner:{index}")
        assert claimed is not None and claimed.current_run_id is not None
        assert claimed.claim_lock is not None
        run_id = int(claimed.current_run_id)
        kb._register_worker_containment(
            conn,
            child_id,
            run_id=run_id,
            claim_lock=claimed.claim_lock,
            worker_pid=700000 + index,
            cgroup_path=f"/sys/fs/cgroup/descendant-{index}",
            cgroup_inode=800000 + index,
        )
        children.append((child_id, run_id))

    legacy_kills = []

    def fake_kill(path, inode):
        reserved = conn.execute(
            "SELECT COUNT(*) AS n FROM worker_containments "
            "WHERE task_id IN (?, ?) AND retirement_reason = 'ancestor_reopened'",
            (children[0][0], children[1][0]),
        ).fetchone()
        assert reserved["n"] == 2
        assert path.startswith("/sys/fs/cgroup/descendant-")
        assert inode in {800000, 800001}
        return {"containment_certified": True, "terminated": True}

    monkeypatch.setattr(kc, "kill_cgroup", fake_kill)
    monkeypatch.setattr(
        kb,
        "_terminate_reclaimed_worker",
        lambda *args, **kwargs: legacy_kills.append(args),
    )

    _reopen_parent_directly(conn, parent_id)
    result = kb.invalidate_descendants_for_parent_reopen(
        conn, parent_id, author="operator",
    )

    assert {entry["id"] for entry in result["invalidated"]} == {
        child_id for child_id, _ in children
    }
    assert legacy_kills == []
    for child_id, run_id in children:
        child = kb.get_task(conn, child_id)
        assert child is not None and child.status == "todo"
        row = conn.execute(
            "SELECT termination_certified_at FROM worker_containments "
            "WHERE task_id = ? AND run_id = ?",
            (child_id, run_id),
        ).fetchone()
        assert row["termination_certified_at"] is not None


def test_counter_reset_on_invalidated_descendants(conn):
    parent_id, child_id = _done_parent_with_done_child(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 4 WHERE id = ?",
            (child_id,),
        )

    _reopen_parent_directly(conn, parent_id)
    kb.invalidate_descendants_for_parent_reopen(conn, parent_id, author="op")

    child = kb.get_task(conn, child_id)
    assert child is not None
    # Deliberate operator action = fresh start with the breaker; contrast
    # with reopen_review_task, which PRESERVES the counter (M2 rule).
    assert child.consecutive_failures == 0


def test_dashboard_and_db_paths_produce_identical_outcomes(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import importlib.util
    import sys

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_m3_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    def build_graph(tag: str):
        with kb.connect() as c:
            parent = kb.create_task(c, title=f"{tag}-parent", assignee="planner")
            assert kb.complete_task(c, parent)
            child = kb.create_task(
                c, title=f"{tag}-child", assignee="builder", parents=[parent],
            )
            assert kb.complete_task(c, child)
        return parent, child

    dash_parent, dash_child = build_graph("dash")
    db_parent, db_child = build_graph("db")

    # Surface 1: dashboard drag (done -> todo) via _set_status_direct.
    r = client.patch(
        f"/api/plugins/kanban/tasks/{dash_parent}", json={"status": "todo"},
    )
    assert r.status_code == 200, r.text

    # Surface 2: DB function directly (the single domain implementation).
    with kb.connect() as c:
        with kb.write_txn(c):
            c.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL "
                "WHERE id = ?",
                (db_parent,),
            )
        kb.invalidate_descendants_for_parent_reopen(
            c, db_parent, author="dashboard",
        )

    with kb.connect() as c:
        def snapshot(tid: str):
            t = kb.get_task(c, tid)
            assert t is not None
            kinds = sorted(e.kind for e in kb.list_events(c, tid))
            n_comments = len(kb.list_comments(c, tid))
            return (
                t.status,
                t.completed_at,
                t.current_run_id,
                t.consecutive_failures,
                kinds,
                n_comments,
            )

        assert snapshot(dash_child) == snapshot(db_child)
        status, completed_at, _run, failures, kinds, n_comments = snapshot(db_child)
        assert status == "todo"
        assert completed_at is None
        assert failures == 0
        assert "descendant_invalidated" in kinds
        assert n_comments >= 1


def test_partial_descendant_retirement_converges_without_reopening_parent(
    monkeypatch,
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        parent = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent)
        children = []
        for index in range(2):
            child = kb.create_task(
                conn,
                title=f"child {index}",
                assignee="builder",
                parents=[parent],
            )
            claimed = kb.claim_task(conn, child, claimer=f"host:child-{index}")
            assert claimed is not None and claimed.current_run_id is not None
            assert claimed.claim_lock is not None
            kb._register_worker_containment(
                conn,
                child,
                run_id=claimed.current_run_id,
                claim_lock=claimed.claim_lock,
                worker_pid=7200 + index,
                cgroup_path=f"/sys/fs/cgroup/hermes-child-partial-{index}",
                cgroup_inode=9200 + index,
            )
            children.append((child, int(claimed.current_run_id)))

        calls = 0

        def partial_kill(_path, _inode):
            nonlocal calls
            calls += 1
            return {
                "terminated": calls == 1,
                "containment_certified": calls == 1,
            }

        monkeypatch.setattr(kc, "kill_cgroup", partial_kill)
        with pytest.raises(kc.ContainmentError):
            kb.invalidate_descendants_for_parent_reopen(conn, parent, author="op")

        parent_task = kb.get_task(conn, parent)
        assert parent_task is not None and parent_task.status == "done"
        rows = conn.execute(
            "SELECT retirement_started_at FROM worker_containments ORDER BY run_id"
        ).fetchall()
        assert len(rows) == 2
        assert all(row["retirement_started_at"] is not None for row in rows)

        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {"terminated": True, "containment_certified": True},
        )
        monkeypatch.setattr(kc, "cgroup_absent", lambda _path: False)
        monkeypatch.setattr(kc, "cleanup_cgroup", lambda *_args: True)
        assert kb.cleanup_inactive_worker_containments(conn) == 2
        parent_task = kb.get_task(conn, parent)
        assert parent_task is not None and parent_task.status == "done"
        for child, run_id in children:
            current = kb.get_task(conn, child)
            assert current is not None and current.status == "ready"
            run = conn.execute(
                "SELECT ended_at, outcome FROM task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            assert run["ended_at"] is not None
            assert run["outcome"] == "reclaimed"
    finally:
        conn.close()
