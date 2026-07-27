"""Behavioral coverage for origin-bound Phase-1 task mutations."""

from __future__ import annotations

import os

import pytest

from agent.kanban_handoff_scope import decide_gateway_origin
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash
from hermes_cli.kanban_control_guard import MANAGED_CONTROL_DENIED_MESSAGE


_IDENTITY = {
    "platform": "feishu",
    "scope_id": "tenant-1",
    "chat_type": "group",
    "chat_id": "group-1",
    "thread_id": "",
    "user_id": "user-1",
    "notifier_profile": "default",
    "session_key": "agent:default:feishu:group:group-1:user-1",
}


@pytest.fixture(autouse=True)
def _fresh_board():
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()


def _task_policy_json() -> str:
    config = {
        "agent": {"max_turns": 90},
        "terminal": {"backend": "local"},
        "kanban": {
            "failure_limit": 2,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_workspace_roots": ["/tmp"],
                "allowed_origins": [
                    {
                        "platform": _IDENTITY["platform"],
                        "chat_type": _IDENTITY["chat_type"],
                        "chat_id": _IDENTITY["chat_id"],
                        "user_id": _IDENTITY["user_id"],
                    }
                ],
            },
        },
    }
    decision = decide_gateway_origin(config, _IDENTITY)
    assert decision["authorized"] is True
    return str(decision["task_policy_json"])


def _create_managed_task(*, board: str | None = None) -> str:
    origin = {
        **_IDENTITY,
        "message_id": "create-1",
        "operation_slot": "slash",
        "short_handoff_policy": _task_policy_json(),
    }
    with kb.connect_closing(board=board) as conn:
        return kb.create_task(
            conn,
            title="origin-bound task",
            assignee="default",
            workspace_kind="dir",
            workspace_path="/tmp",
            control_origin=origin,
        )


def _comment_count(task_id: str) -> int:
    with kb.connect_closing() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )


def test_same_bound_group_and_user_can_mutate_managed_task():
    task_id = _create_managed_task()

    output = run_slash(
        f"comment {task_id} 同群操作成功",
        mutation_identity=dict(_IDENTITY),
    )

    assert output == f"Comment added to {task_id}"
    assert _comment_count(task_id) == 1


@pytest.mark.parametrize(
    ("surface", "identity"),
    [
        ("app", None),
        ("tui", None),
        ("other_group", {**_IDENTITY, "chat_id": "group-2"}),
        (
            "direct_message",
            {**_IDENTITY, "chat_type": "dm", "chat_id": "dm-user-1"},
        ),
        ("other_user", {**_IDENTITY, "user_id": "user-2"}),
        (
            "codingman",
            {
                **_IDENTITY,
                "notifier_profile": "coding-man",
                "session_key": "agent:coding-man:feishu:group:group-1:user-1",
            },
        ),
    ],
)
def test_other_surfaces_cannot_mutate_managed_task(surface, identity):
    task_id = _create_managed_task()

    output = run_slash(
        f"comment {task_id} 不应写入-{surface}",
        mutation_identity=identity,
    )

    assert output == MANAGED_CONTROL_DENIED_MESSAGE
    assert _comment_count(task_id) == 0


def test_managed_task_stays_readable_from_unbound_surfaces():
    task_id = _create_managed_task()

    shown = run_slash(f"show {task_id}")
    listed = run_slash("list")

    assert "origin-bound task" in shown
    assert task_id in listed


@pytest.mark.parametrize("command", ["dispatch --dry-run", "daemon --force"])
def test_dispatch_surfaces_cannot_reach_active_managed_tasks(command):
    _create_managed_task()

    output = run_slash(command)

    assert output == MANAGED_CONTROL_DENIED_MESSAGE


@pytest.mark.parametrize(
    ("command", "handler_name"),
    [
        ("dispatch --dry-run", "_cmd_dispatch"),
        ("daemon --force", "_cmd_daemon"),
    ],
)
def test_completed_managed_history_does_not_block_ordinary_dispatch_surfaces(
    command, handler_name, monkeypatch
):
    managed_id = _create_managed_task()
    with kb.connect_closing() as conn:
        ordinary_id = kb.create_task(
            conn,
            title="ordinary follow-up",
            assignee="default",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = 1 WHERE id = ?",
                (managed_id,),
            )

    called = []

    def fake_handler(_args):
        called.append(ordinary_id)
        return 0

    monkeypatch.setattr(kanban_cli, handler_name, fake_handler)

    output = run_slash(command)

    assert output == "(no output)"
    assert called == [ordinary_id]


@pytest.mark.parametrize(
    ("command", "identity"),
    [
        ("boards rm protected-archive", None),
        ("boards delete protected-delete", dict(_IDENTITY)),
    ],
)
def test_whole_board_removal_preserves_managed_evidence(command, identity):
    slug = command.rsplit(" ", 1)[-1]
    kb.create_board(slug)
    kb.init_db(board=slug)
    task_id = _create_managed_task(board=slug)

    output = run_slash(command, mutation_identity=identity)

    assert "不能整板删除或归档" in output
    assert kb.board_exists(slug)
    with kb.connect_closing(board=slug) as conn:
        assert kb.get_task(conn, task_id) is not None


def test_gc_preserves_managed_evidence_and_cleans_ordinary_history():
    managed_id = _create_managed_task()
    scratch_root = kb.workspaces_root()
    with kb.connect_closing() as conn:
        ordinary_id = kb.create_task(conn, title="ordinary archived task")
        managed_workspace = scratch_root / managed_id
        ordinary_workspace = scratch_root / ordinary_id
        managed_workspace.mkdir(parents=True)
        ordinary_workspace.mkdir(parents=True)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'archived', workspace_kind = 'scratch', "
                "workspace_path = ? WHERE id = ?",
                (str(managed_workspace), managed_id),
            )
            conn.execute(
                "UPDATE tasks SET status = 'archived', workspace_kind = 'scratch', "
                "workspace_path = ? WHERE id = ?",
                (str(ordinary_workspace), ordinary_id),
            )
            conn.execute(
                "UPDATE task_events SET created_at = 1 WHERE task_id IN (?, ?)",
                (managed_id, ordinary_id),
            )

    managed_log = kb.worker_log_path(managed_id)
    ordinary_log = kb.worker_log_path(ordinary_id)
    managed_log.parent.mkdir(parents=True, exist_ok=True)
    managed_log.write_text("managed evidence", encoding="utf-8")
    ordinary_log.write_text("ordinary history", encoding="utf-8")
    os.utime(managed_log, (1, 1))
    os.utime(ordinary_log, (1, 1))

    output = run_slash(
        "gc --event-retention-days 0 --log-retention-days 0"
    )

    assert "GC complete" in output
    assert managed_workspace.exists()
    assert not ordinary_workspace.exists()
    assert managed_log.exists()
    assert not ordinary_log.exists()
    with kb.connect_closing() as conn:
        managed_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (managed_id,),
        ).fetchone()[0]
        ordinary_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (ordinary_id,),
        ).fetchone()[0]
    assert managed_events > 0
    assert ordinary_events == 0


def test_ordinary_task_mutation_is_unchanged_without_gateway_identity():
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ordinary task")

    output = run_slash(f"comment {task_id} 普通任务仍可操作")

    assert output == f"Comment added to {task_id}"
    assert _comment_count(task_id) == 1


def test_create_idempotency_cannot_retarget_an_existing_managed_task():
    task_id = _create_managed_task()
    # Automatic successors carry an internal idempotency key after the frozen
    # control binding moves to them. Model that durable state directly.
    with kb.connect_closing() as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET idempotency_key = ? WHERE id = ?",
            ("managed-key", task_id),
        )

    output = run_slash(
        'create "different request" --idempotency-key managed-key'
    )

    assert output == MANAGED_CONTROL_DENIED_MESSAGE
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        assert [task.id for task in tasks] == [task_id]
        assert tasks[0].title == "origin-bound task"
