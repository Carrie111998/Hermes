"""Regression tests for #27145 — kanban.default_assignee for unassigned ready tasks.

When the dispatcher hits an unassigned ready task and ``kanban.default_assignee``
is set, the dispatcher applies the assignment and spawns. Without the config,
the task is skipped (existing behavior preserved).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_default_assignee_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Force-reimport so the fresh HERMES_HOME is picked up.
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home
    # Cleanup is best-effort; tempfile dir survives but pytest isolation
    # gives each test its own monkeypatched HERMES_HOME so no cross-test
    # contamination.


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345




def test_unassigned_task_auto_assigned_with_default_assignee(isolated_kanban_home):
    """Core #27145 contract: with default_assignee set, an unassigned ready
    task gets the assignment applied and dispatched on the same tick. The
    DB row is mutated (assignee column + an 'assigned' event)."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
            default_assignee_dispatcher_profile="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert not res.skipped_unassigned
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.spawned[0][1] == "default"

    with kb.connect_closing() as conn:
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["assignee"] == "default"

    # 'assigned' event emitted for the audit trail
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ))
    assert len(evs) == 1
    payload = json.loads(evs[0][1])
    assert payload["assignee"] == "default"
    assert payload["source"] == "kanban.default_assignee"
    assert payload["dispatcher_profile"] == "default"
    assert payload["routing_rule"] == "kanban.default_assignee_boards:default"






def test_explicitly_assigned_task_untouched_by_default_assignee(isolated_kanban_home):
    """A task with an explicit assignee must NOT be touched by the
    default_assignee logic — that fallback only applies to genuinely
    unassigned rows."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="someother",
        )
    assert task_id not in res.auto_assigned_default
    assert any(s[0] == task_id and s[1] == "default" for s in res.spawned)


@pytest.mark.parametrize(
    ("board", "configured_boards", "expected"),
    [
        ("default", None, None),
        ("project-a", None, None),
        ("default", [], None),
        ("project-a", ["project-a"], "kanban.default_assignee_boards:project-a"),
        ("project-a", ["PROJECT-A"], "kanban.default_assignee_boards:project-a"),
        ("project-a", ["*"], "kanban.default_assignee_boards:*"),
        ("project-a", "project-a", None),
        ("none", [None], None),
        ("123", [123], None),
        ("project-a", ["../project-a"], None),
    ],
)
def test_default_assignee_board_routing_contract(
    isolated_kanban_home,
    board,
    configured_boards,
    expected,
):
    """Fallback routing is explicit, with a narrow legacy-default migration."""
    kb, _home = isolated_kanban_home
    assert kb.default_assignee_routing_rule(board, configured_boards) == expected


def test_default_config_preserves_legacy_default_board_only(isolated_kanban_home):
    """Missing user config keeps #27145 on default without authorizing names."""
    _kb, home = isolated_kanban_home
    from hermes_cli.config import load_config

    config_path = Path(home) / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"kanban": {"default_assignee": "default"}}),
        encoding="utf-8",
    )
    assert load_config()["kanban"]["default_assignee_boards"] == ["default"]

    config_path.write_text(
        yaml.safe_dump(
            {
                "kanban": {
                    "default_assignee": "default",
                    "default_assignee_boards": [],
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_config()["kanban"]["default_assignee_boards"] == []

def test_dispatch_enforces_board_routing_at_assignment_boundary(
    isolated_kanban_home,
):
    """Authorization follows the connection, not an optional caller hint."""
    kb, _home = isolated_kanban_home
    kb.create_board("project-a")
    with kb.connect_closing(board="project-a") as conn:
        task_id = kb.create_task(conn, title="named-board task", assignee=None)

        # Omitting board= must not make this named-board connection inherit the
        # ambient default board's fallback authorization.
        denied = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            default_assignee="default",
        )
        assert denied.skipped_unassigned == [task_id]
        untouched = kb.get_task(conn, task_id)
        assert untouched is not None and untouched.assignee is None
        assert conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ).fetchone() is None

        allowed = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            default_assignee="default",
            default_assignee_dispatcher_profile="default",
            default_assignee_boards=["project-a"],
        )

        task = kb.get_task(conn, task_id)
        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'assigned'",
            (task_id,),
        ).fetchone()

    assert allowed.auto_assigned_default == [task_id]
    assert task is not None and task.assignee == "default"
    payload = json.loads(event["payload"])
    assert payload["dispatcher_profile"] == "default"
    assert payload["routing_rule"] == "kanban.default_assignee_boards:project-a"


def test_dispatch_ignores_mismatched_board_hint_for_fallback_authorization(
    isolated_kanban_home,
):
    """Connection identity governs authorization and downstream spawn routing."""
    kb, _home = isolated_kanban_home
    kb.create_board("project-a")
    spawned_boards = []

    def _recording_spawn(*_args, board=None, **_kwargs):
        spawned_boards.append(board)
        return 12345

    with kb.connect_closing(board="project-a") as conn:
        task_id = kb.create_task(conn, title="mismatched board hint", assignee=None)
        result = kb.dispatch_once(
            conn,
            board="default",
            spawn_fn=_fake_spawn,
            default_assignee="default",
        )
        task = kb.get_task(conn, task_id)

        assert result.skipped_unassigned == [task_id]
        assert result.auto_assigned_default == []
        assert task is not None and task.assignee is None

        allowed = kb.dispatch_once(
            conn,
            board="default",
            spawn_fn=_recording_spawn,
            default_assignee="default",
            default_assignee_boards=["project-a"],
        )
        task = kb.get_task(conn, task_id)

    assert allowed.auto_assigned_default == [task_id]
    assert task is not None and task.assignee == "default"
    assert spawned_boards == ["project-a"]


def test_custom_db_path_requires_explicit_legacy_pin_for_fallback(
    isolated_kanban_home,
    monkeypatch,
    tmp_path,
):
    """Unknown DB paths fail closed, while HERMES_KANBAN_DB stays compatible."""
    kb, _home = isolated_kanban_home
    custom_db = tmp_path / "custom-board.sqlite"
    with kb.connect(db_path=custom_db) as conn:
        task_id = kb.create_task(conn, title="custom DB fallback", assignee=None)

        denied = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            default_assignee="default",
        )
        task = kb.get_task(conn, task_id)
        assert denied.skipped_unassigned == [task_id]
        assert task is not None and task.assignee is None

        monkeypatch.setenv("HERMES_KANBAN_DB", str(custom_db))
        allowed = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            default_assignee="default",
        )
        task = kb.get_task(conn, task_id)

    assert allowed.auto_assigned_default == [task_id]
    assert task is not None and task.assignee == "default"
