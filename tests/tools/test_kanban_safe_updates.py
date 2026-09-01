from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def orchestrator_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    monkeypatch.setattr(kt, "_kanban_profile_exists", lambda _name: True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_update_reassigns_default_card_with_audit_event(orchestrator_board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route me", assignee="default")

    result = json.loads(
        kt._handle_update(
            {
                "action": "reassign",
                "task_id": task_id,
                "assignee": "reviewer",
                "reason": "route to real profile",
            }
        )
    )

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "reviewer"
        event = kb.list_events(conn, task_id)[-1]
        assert event.kind == "assigned"
        assert event.payload == {
            "assignee": "reviewer",
            "reason": "route to real profile",
        }


def test_update_returns_canonical_reassigned_profile(orchestrator_board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route me", assignee="default")

    result = json.loads(
        kt._handle_update(
            {
                "action": "reassign",
                "task_id": task_id,
                "assignee": " Reviewer ",
                "reason": "route",
            }
        )
    )

    assert result["assignee"] == "reviewer"
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == result["assignee"]


def test_update_moves_deferred_card_to_triage(orchestrator_board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="later", assignee="default")

    result = json.loads(
        kt._handle_update(
            {"action": "triage", "task_id": task_id, "reason": "deferred"}
        )
    )

    assert result == {"ok": True, "task_id": task_id, "status": "triage"}
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "triage"


def test_update_refuses_to_hide_human_blocker(orchestrator_board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval", assignee="default")
        kb.block_task(conn, task_id, reason="approval", kind="needs_input")

    result = json.loads(
        kt._handle_update(
            {"action": "triage", "task_id": task_id, "reason": "cleanup"}
        )
    )

    assert "blocked" in result["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"


def test_update_supersede_readback_is_archived_not_done(orchestrator_board):
    with kb.connect() as conn:
        obsolete = kb.create_task(conn, title="duplicate", assignee="default")

    result = json.loads(
        kt._handle_update(
            {"action": "supersede", "task_id": obsolete, "reason": "duplicate"}
        )
    )

    assert result["ok"] is True
    assert result["status"] == "archived"
    with kb.connect() as conn:
        assert kb.get_task(conn, obsolete).status == "archived"
        assert kb.list_events(conn, obsolete)[-1].kind == "superseded"


def test_update_schema_is_orchestrator_only():
    assert kt.KANBAN_UPDATE_SCHEMA["name"] == "kanban_update"
    assert "orchestrator-only" in kt.KANBAN_UPDATE_SCHEMA["description"].lower()


def test_update_runtime_guard_rejects_ordinary_profile(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: False)

    result = json.loads(
        kt._handle_update(
            {"action": "triage", "task_id": "t_any", "reason": "defer"}
        )
    )

    assert "orchestrator-only" in result["error"]


def test_update_runtime_guard_rejects_delegated_child(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    monkeypatch.setattr(kt, "_is_delegated_child_context", lambda: True)

    result = json.loads(
        kt._handle_update(
            {"action": "triage", "task_id": "t_any", "reason": "defer"}
        )
    )

    assert "orchestrator-only" in result["error"]


def test_update_reassign_rejects_unknown_profile(orchestrator_board, monkeypatch):
    monkeypatch.setattr(kt, "_kanban_profile_exists", lambda _name: False)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route me", assignee="default")

    result = json.loads(
        kt._handle_update(
            {
                "action": "reassign",
                "task_id": task_id,
                "assignee": "ghost",
                "reason": "route",
            }
        )
    )

    assert "unknown profile" in result["error"]


def test_update_reassign_rejects_default_alias(orchestrator_board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route me", assignee="reviewer")

    result = json.loads(
        kt._handle_update(
            {
                "action": "reassign",
                "task_id": task_id,
                "assignee": " Default ",
                "reason": "route",
            }
        )
    )

    assert "real profile" in result["error"]


def test_update_notifies_relinked_child(orchestrator_board, monkeypatch):
    updates = []
    monkeypatch.setattr(
        kb,
        "notify_task_updated",
        lambda _conn, tid, fields: updates.append((tid, fields)),
    )
    with kb.connect() as conn:
        obsolete = kb.create_task(conn, title="obsolete", assignee="default")
        replacement = kb.create_task(conn, title="replacement", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=[obsolete]
        )
    updates.clear()

    result = json.loads(
        kt._handle_update(
            {
                "action": "supersede",
                "task_id": obsolete,
                "replacement_task_id": replacement,
                "reason": "duplicate",
            }
        )
    )

    assert result["ok"] is True
    assert (child, ("parents", "status")) in updates
