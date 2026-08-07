"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"








# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kb.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


# ---------------------------------------------------------------------------
# Forced promotion claim authorization
# ---------------------------------------------------------------------------


def _authorization_rows(conn, task_id):
    return conn.execute(
        "SELECT * FROM forced_claim_authorizations WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()


def test_open_parents_reject_ordinary_promotion_and_claim(conn):
    child, parents = _stuck_todo(conn, parents_done=False)

    ok, err = kb.promote_task(conn, child, actor="operator")
    assert not ok
    assert parents[0] in (err or "")
    assert _authorization_rows(conn, child) == []

    # The claim boundary remains fail-closed even if another writer puts the
    # task in ready without a force authorization.
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    assert kb.claim_task(conn, child, claimer="ordinary") is None
    assert kb.get_task(conn, child).status == "todo"
    rejected = [e for e in kb.list_events(conn, child) if e.kind == "claim_rejected"]
    assert rejected[-1].payload["reason"] == "parents_not_done"


def test_forced_promotion_survives_connection_boundary_and_claims_once(kanban_home):
    with kb.connect() as conn:
        child, parents = _stuck_todo(conn, parents_done=False, n_parents=2)
        ok, err = kb.promote_task(
            conn, child, actor="operator", reason="reviewed staged handoff", force=True
        )
        assert ok and err is None
        auth = _authorization_rows(conn, child)[0]
        assert json.loads(auth["authorized_parent_ids"]) == sorted(parents)
        assert auth["actor"] == "operator"
        assert auth["reason"] == "reviewed staged handoff"
        assert auth["consumed_at"] is None

    # A fresh connection models the dispatcher process, proving the authority
    # is durable rather than an in-memory prompt/flag bypass.
    with kb.connect() as conn:
        claimed = kb.claim_task(conn, child, claimer="dispatcher-1")
        assert claimed is not None
        assert claimed.status == "running"
        auth = _authorization_rows(conn, child)[0]
        assert auth["consumed_at"] is not None
        assert auth["consumed_by"] == "dispatcher-1"
        assert auth["consumed_run_id"] == claimed.current_run_id
        claimed_event = [e for e in kb.list_events(conn, child) if e.kind == "claimed"][-1]
        assert claimed_event.payload["forced_parent_override"] is True
        assert claimed_event.payload["overridden_parent_ids"] == sorted(parents)


def test_consumed_force_authorization_cannot_be_replayed(conn):
    child, _ = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    first = kb.claim_task(conn, child, claimer="first")
    assert first is not None

    # Model a crash/requeue ready epoch without issuing a fresh --force. The
    # consumed grant must not authorize a retry.
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "current_run_id=NULL WHERE id=?",
        (child,),
    )
    assert kb.claim_task(conn, child, claimer="replay") is None
    assert kb.get_task(conn, child).status == "todo"
    rows = _authorization_rows(conn, child)
    assert len(rows) == 1 and rows[0]["consumed_by"] == "first"


def test_concurrent_claimers_have_one_winner_and_one_atomic_consumption(kanban_home):
    with kb.connect() as conn:
        child, _ = _stuck_todo(conn, parents_done=False)
        assert kb.promote_task(conn, child, actor="operator", force=True)[0]

    barrier = threading.Barrier(2)

    def claim(name):
        with kb.connect() as worker_conn:
            barrier.wait(timeout=5)
            claimed = kb.claim_task(worker_conn, child, claimer=name)
            return claimed.id if claimed else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("worker-a", "worker-b")))

    assert outcomes.count(child) == 1
    assert outcomes.count(None) == 1
    with kb.connect() as conn:
        auth = _authorization_rows(conn, child)[0]
        assert auth["consumed_by"] in {"worker-a", "worker-b"}
        assert auth["consumed_run_id"] is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (child,)
        ).fetchone()[0] == 1


def test_force_dry_runs_do_not_create_or_consume_authorization(
    conn, kanban_home, monkeypatch
):
    child, _ = _stuck_todo(conn, parents_done=False)
    event_count = len(kb.list_events(conn, child))
    assert kb.promote_task(
        conn, child, actor="operator", force=True, dry_run=True
    ) == (True, None)
    assert kb.get_task(conn, child).status == "todo"
    assert _authorization_rows(conn, child) == []
    assert len(kb.list_events(conn, child)) == event_count

    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    auth_before = dict(_authorization_rows(conn, child)[0])
    tasks_before = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
    events_before = [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")]
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    result = kb.dispatch_once(conn, dry_run=True, spawn_fn=lambda *_args: 123)
    assert child in [item[0] for item in result.spawned]
    assert [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == tasks_before
    assert dict(_authorization_rows(conn, child)[0]) == auth_before
    assert [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")] == events_before


def test_force_authorization_allows_dependencies_that_later_finish(conn):
    child, parents = _stuck_todo(conn, parents_done=False, n_parents=2)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parents[0],))

    claimed = kb.claim_task(conn, child, claimer="worker")
    assert claimed is not None
    claimed_event = [e for e in kb.list_events(conn, child) if e.kind == "claimed"][-1]
    assert claimed_event.payload["overridden_parent_ids"] == [parents[1]]


def test_new_open_dependency_invalidates_force_authorization(conn):
    child, _ = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    new_parent = kb.create_task(conn, title="new parent")

    # Official edge mutation immediately demotes and invalidates the old grant.
    kb.link_tasks(conn, new_parent, child)
    auth = _authorization_rows(conn, child)[0]
    assert kb.get_task(conn, child).status == "todo"
    assert auth["invalidated_at"] is not None

    # Even an external writer restoring ready cannot revive it.
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    assert kb.claim_task(conn, child, claimer="worker") is None


def test_parent_reopened_after_force_is_not_implicitly_authorized(conn):
    child, parents = _stuck_todo(conn, parents_done=False, n_parents=2)
    # Only parent[0] is open when the force grant is minted.
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parents[1],))
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parents[1],))

    assert kb.claim_task(conn, child, claimer="worker") is None
    auth = _authorization_rows(conn, child)[0]
    assert auth["invalidated_at"] is not None
    rejected = [e for e in kb.list_events(conn, child) if e.kind == "claim_rejected"][-1]
    assert rejected.payload["unexpected_parent_ids"] == [parents[1]]


def test_dispatch_dry_run_rejects_stale_force_without_mutation(conn, monkeypatch):
    child, parents = _stuck_todo(conn, parents_done=False, n_parents=2)
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parents[1],))
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parents[1],))
    before = list(conn.iterdump())
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    result = kb.dispatch_once(conn, dry_run=True, spawn_fn=lambda *_args: 123)

    assert child not in [item[0] for item in result.spawned]
    assert list(conn.iterdump()) == before


def test_force_authorization_is_invalidated_when_task_leaves_ready(conn):
    child, _ = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    assert kb.block_task(conn, child, reason="pause before claim")
    auth = _authorization_rows(conn, child)[0]
    assert auth["invalidated_at"] is not None

    # Even a raw writer restoring ready cannot replay authority from the old
    # ready epoch; the trigger is the durable fence for every writer path.
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    assert kb.claim_task(conn, child, claimer="later") is None


def test_dispatch_dry_run_previews_automatic_promotion_without_mutation(
    conn, monkeypatch
):
    child, parents = _stuck_todo(conn, parents_done=False)
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parents[0],))
    before = list(conn.iterdump())
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    result = kb.dispatch_once(conn, dry_run=True, spawn_fn=lambda *_args: 123)

    assert result.promoted == 1
    assert child in [item[0] for item in result.spawned]
    assert list(conn.iterdump()) == before


def test_removed_open_dependency_needs_no_broader_bypass(conn):
    child, parents = _stuck_todo(conn, parents_done=False, n_parents=2)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    assert kb.unlink_tasks(conn, parents[0], child)

    claimed = kb.claim_task(conn, child, claimer="worker")
    assert claimed is not None
    event = [e for e in kb.list_events(conn, child) if e.kind == "claimed"][-1]
    assert event.payload["overridden_parent_ids"] == [parents[1]]


def test_raw_ready_running_ready_invalidates_unconsumed_force(conn):
    child, _ = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]

    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))

    auth = _authorization_rows(conn, child)[0]
    assert auth["consumed_at"] is None
    assert auth["invalidated_at"] is not None
    assert kb.claim_task(conn, child, claimer="replay") is None


def test_raw_task_delete_and_id_reuse_cannot_inherit_force(conn):
    child, _ = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    conn.execute("CREATE TEMP TABLE saved_task AS SELECT * FROM tasks WHERE id=?", (child,))
    conn.execute("DELETE FROM tasks WHERE id=?", (child,))
    conn.execute("INSERT INTO tasks SELECT * FROM saved_task")

    auth = _authorization_rows(conn, child)[0]
    assert auth["invalidated_at"] is not None
    assert kb.claim_task(conn, child, claimer="replacement") is None


def test_dangling_parent_link_blocks_ordinary_and_forced_claims(conn):
    child = kb.create_task(conn, title="child", assignee="setup")
    conn.execute(
        "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
        ("t_missing", child),
    )

    assert kb.claim_task(conn, child, claimer="ordinary") is None
    task = kb.get_task(conn, child)
    assert task is not None and task.status == "todo"
    rejected = [e for e in kb.list_events(conn, child) if e.kind == "claim_rejected"][-1]
    assert rejected.payload is not None
    assert rejected.payload["reason"] == "malformed_dependencies"
    assert rejected.payload["missing_parent_ids"] == ["t_missing"]

    ok, error = kb.promote_task(conn, child, actor="operator", force=True)
    assert not ok
    assert "missing parent tasks" in (error or "")
    assert _authorization_rows(conn, child) == []


def test_noncanonical_duplicate_parent_authorization_fails_closed(conn):
    child, parents = _stuck_todo(conn, parents_done=False)
    assert kb.promote_task(conn, child, actor="operator", force=True)[0]
    conn.execute(
        "UPDATE forced_claim_authorizations SET authorized_parent_ids=? "
        "WHERE task_id=? AND consumed_at IS NULL AND invalidated_at IS NULL",
        (json.dumps([parents[0], parents[0]]), child),
    )

    assert kb.claim_task(conn, child, claimer="malformed") is None
    task = kb.get_task(conn, child)
    assert task is not None and task.status == "todo"
    auth = _authorization_rows(conn, child)[0]
    assert auth["invalidated_at"] is not None
    assert auth["invalidation_reason"] == "malformed authorization"
