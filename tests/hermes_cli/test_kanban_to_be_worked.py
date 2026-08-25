"""Behavior contract for the Marcin-owned ``to_be_worked`` shelf.

The standing leftover index t_7f646b36 is deliberately out of scope: this
suite creates isolated temporary cards and performs no migration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "unknown")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    connection = kb.connect()
    try:
        yield connection
    finally:
        connection.close()


def _event(conn, task_id: str, kind: str):
    return [event for event in kb.list_events(conn, task_id) if event.kind == kind][-1]


def _spawn_spy(calls: list[str]):
    def spawn(task, _workspace, board=None):
        calls.append(task.id)
        return 4242

    return spawn


def test_e1_e2_create_into_shelf_is_direct_and_never_claimed(conn):
    task_id = kb.create_task(
        conn,
        title="decide later",
        assignee="default",
        initial_status="to_be_worked",
    )

    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "to_be_worked"
    events = kb.list_events(conn, task_id)
    assert [event.kind for event in events] == ["created"]
    assert events[0].payload["status"] == "to_be_worked"

    calls: list[str] = []
    first = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))
    second = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))
    assert first.spawned == []
    assert second.spawned == []
    assert calls == []
    assert kb.claim_task(conn, task_id) is None
    assert kb.has_spawnable_ready(conn) is False
    assert kb.has_spawnable_review(conn) is False
    assert kb.get_task(conn, task_id).status == "to_be_worked"


def test_create_shelf_rejects_conflicting_triage_landing(conn):
    with pytest.raises(ValueError, match="triage"):
        kb.create_task(
            conn,
            title="one landing only",
            triage=True,
            initial_status="to_be_worked",
        )


def test_e3_default_ready_path_still_dispatches(conn):
    task_id = kb.create_task(conn, title="hot door", assignee="default")
    calls: list[str] = []

    result = kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))

    assert [task_id] == calls
    assert [entry[0] for entry in result.spawned] == [task_id]
    assert kb.get_task(conn, task_id).status == "running"


def test_e4_blocked_remains_sticky_and_unblock_still_resumes(conn):
    task_id = kb.create_task(conn, title="actually stuck", assignee="default")
    assert kb.block_task(conn, task_id, reason="need input")
    assert kb.get_task(conn, task_id).status == "blocked"
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, task_id).status == "blocked"

    assert kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == "ready"


def test_e5_e6_blocked_to_shelf_is_not_unblock_or_promote(conn):
    task_id = kb.create_task(conn, title="think about it", assignee="default")
    assert kb.block_task(conn, task_id, reason="not a start")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 2 WHERE id = ?",
            (task_id,),
        )

    ok, reason = kb.shelf_task(
        conn, task_id, actor="marcin", reason="named for the shelf"
    )
    assert ok, reason
    assert kb.get_task(conn, task_id).status == "to_be_worked"
    shelved = _event(conn, task_id, "shelved")
    assert shelved.payload == {
        "actor": "marcin",
        "from_status": "blocked",
        "reason": "named for the shelf",
    }

    assert kb.unblock_task(conn, task_id) is False
    promoted, promote_reason = kb.promote_task(conn, task_id, actor="marcin")
    assert promoted is False
    assert "todo" in promote_reason and "blocked" in promote_reason
    after = kb.get_task(conn, task_id)
    assert after.status == "to_be_worked"
    assert after.consecutive_failures == 2

    calls: list[str] = []
    assert kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls)).spawned == []
    assert calls == []


@pytest.mark.parametrize("source", ["todo", "scheduled", "running", "review", "done"])
def test_shelf_task_refuses_unlocked_source_statuses(conn, source):
    task_id = kb.create_task(conn, title=f"not from {source}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (source, task_id))

    ok, reason = kb.shelf_task(conn, task_id, actor="marcin")

    assert ok is False
    assert source in reason
    assert kb.get_task(conn, task_id).status == source


def test_e7_parent_completion_does_not_promote_shelf_child(conn):
    parent = kb.create_task(conn, title="parent")
    shelf_child = kb.create_task(
        conn,
        title="shelf child",
        parents=[parent],
        initial_status="to_be_worked",
    )
    control_child = kb.create_task(conn, title="normal child", parents=[parent])
    assert kb.get_task(conn, shelf_child).status == "to_be_worked"
    assert kb.get_task(conn, control_child).status == "todo"

    assert kb.complete_task(conn, parent)
    kb.recompute_ready(conn)

    assert kb.get_task(conn, shelf_child).status == "to_be_worked"
    assert kb.get_task(conn, control_child).status == "ready"
    calls: list[str] = []
    kb.dispatch_once(conn, spawn_fn=_spawn_spy(calls))
    assert shelf_child not in calls


def test_e8_unshelf_is_the_only_exit_and_parent_gates_ready(conn):
    parent = kb.create_task(conn, title="parent")
    gated = kb.create_task(
        conn,
        title="gated shelf",
        parents=[parent],
        initial_status="to_be_worked",
    )

    ok, reason = kb.unshelf_task(conn, gated, actor="marcin", dest="ready")
    assert ok, reason
    assert kb.get_task(conn, gated).status == "todo"
    assert _event(conn, gated, "unshelved").payload == {
        "actor": "marcin",
        "dest": "todo",
        "reason": None,
    }

    triage_task = kb.create_task(
        conn, title="triage shelf", initial_status="to_be_worked"
    )
    ok, reason = kb.unshelf_task(
        conn, triage_task, actor="marcin", dest="triage", reason="needs shaping"
    )
    assert ok, reason
    assert kb.get_task(conn, triage_task).status == "triage"

    refused = kb.create_task(
        conn, title="cannot start", initial_status="to_be_worked"
    )
    ok, reason = kb.unshelf_task(conn, refused, actor="marcin", dest="running")
    assert ok is False
    assert "triage" in reason and "ready" in reason
    assert kb.get_task(conn, refused).status == "to_be_worked"

    assert kb.complete_task(conn, parent)
    ready_task = kb.create_task(
        conn,
        title="parents done",
        parents=[parent],
        initial_status="to_be_worked",
    )
    ok, reason = kb.unshelf_task(conn, ready_task, actor="marcin", dest="ready")
    assert ok, reason
    assert kb.get_task(conn, ready_task).status == "ready"


def test_e9_list_and_cli_surface_the_shelf(conn, monkeypatch, capsys):
    task_id = kb.create_task(
        conn,
        title="visible shelf",
        assignee="default",
        initial_status="to_be_worked",
    )
    assert [task.id for task in kb.list_tasks(conn, status="to_be_worked")] == [task_id]

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    create_args = root.parse_args(
        ["kanban", "create", "cli shelf", "--initial-status", "to_be_worked"]
    )
    assert create_args.initial_status == "to_be_worked"

    stats_args = argparse.Namespace(json=False)
    assert kanban_cli._cmd_stats(stats_args) == 0
    assert "to_be_worked" in capsys.readouterr().out


def test_cli_shelf_unshelf_and_unblock_refusal(conn, monkeypatch, capsys):
    task_id = kb.create_task(conn, title="cli move")
    assert kb.block_task(conn, task_id, reason="pause")
    monkeypatch.setattr(kanban_cli, "_profile_author", lambda: "marcin")

    shelf_args = argparse.Namespace(task_id=task_id, reason="think")
    assert kanban_cli._cmd_shelf(shelf_args) == 0
    assert kb.get_task(conn, task_id).status == "to_be_worked"

    unblock_args = argparse.Namespace(task_ids=[task_id], reason=None)
    assert kanban_cli._cmd_unblock(unblock_args) == 1
    assert kb.get_task(conn, task_id).status == "to_be_worked"

    unshelf_args = argparse.Namespace(
        task_id=task_id, dest="triage", reason="shape it"
    )
    assert kanban_cli._cmd_unshelf(unshelf_args) == 0
    assert kb.get_task(conn, task_id).status == "triage"
    assert "cannot unblock" in capsys.readouterr().err


def test_worker_schema_can_create_and_list_but_cannot_unshelf():
    from tools import kanban_tools

    create_statuses = set(
        kanban_tools.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
        ["initial_status"]["enum"]
    )
    list_statuses = set(
        kanban_tools.KANBAN_LIST_SCHEMA["parameters"]["properties"]["status"]["enum"]
    )
    assert "to_be_worked" in create_statuses
    assert {"scheduled", "review", "to_be_worked"} <= list_statuses
    registered_names = {
        schema["name"]
        for schema in vars(kanban_tools).values()
        if isinstance(schema, dict) and isinstance(schema.get("name"), str)
    }
    assert "kanban_unshelf" not in registered_names


def test_e11_dispatch_selects_never_include_the_shelf_status(conn):
    task_id = kb.create_task(
        conn,
        title="query inventory shelf",
        assignee="default",
        initial_status="to_be_worked",
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        assert kb.claim_task(conn, task_id) is None
        assert kb.has_spawnable_ready(conn) is False
        assert kb.has_spawnable_review(conn) is False
        assert kb.dispatch_once(
            conn, spawn_fn=_spawn_spy([]),
        ).spawned == []
    finally:
        conn.set_trace_callback(None)

    selects = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert selects
    assert all("to_be_worked" not in statement for statement in selects)
