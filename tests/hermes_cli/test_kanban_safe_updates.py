from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def test_supersede_refuses_to_release_unhandled_children(board):
    obsolete = kb.create_task(board, title="obsolete", assignee="default")
    child = kb.create_task(
        board, title="child", assignee="worker", parents=[obsolete]
    )

    with pytest.raises(RuntimeError, match="replacement"):
        kb.supersede_task(board, obsolete, reason="duplicate")

    assert kb.get_task(board, obsolete).status == "ready"
    assert kb.get_task(board, child).status == "todo"


def test_supersede_relinks_children_and_records_audit_event(board):
    obsolete = kb.create_task(board, title="obsolete", assignee="default")
    replacement = kb.create_task(board, title="replacement", assignee="worker")
    child = kb.create_task(
        board, title="child", assignee="worker", parents=[obsolete]
    )

    result = kb.supersede_task(
        board,
        obsolete,
        replacement_task_id=replacement,
        reason="deduplicated backlog",
    )

    assert result["replacement_task_id"] == replacement
    assert result["relinked_children"] == [child]
    assert kb.get_task(board, obsolete).status == "archived"
    assert kb.get_task(board, child).status == "todo"
    assert replacement in kb.parent_ids(board, child)
    event = kb.list_events(board, obsolete)[-1]
    assert event.kind == "superseded"
    assert event.payload == {
        "reason": "deduplicated backlog",
        "replacement_task_id": replacement,
        "relinked_parent_ids": [],
        "relinked_children": [child],
        "open_parent_ids": [],
    }


def test_supersede_reports_open_parents(board):
    upstream = kb.create_task(board, title="upstream", assignee="worker")
    obsolete = kb.create_task(
        board, title="obsolete", assignee="default", parents=[upstream]
    )

    result = kb.supersede_task(board, obsolete, reason="duplicate")

    assert result["open_parent_ids"] == [upstream]


def test_supersede_preserves_open_parents_on_replacement(board):
    upstream = kb.create_task(board, title="upstream", assignee="worker")
    obsolete = kb.create_task(
        board, title="obsolete", assignee="default", parents=[upstream]
    )
    replacement = kb.create_task(board, title="replacement", assignee="worker")

    result = kb.supersede_task(
        board,
        obsolete,
        reason="duplicate",
        replacement_task_id=replacement,
    )

    assert result["relinked_parent_ids"] == [upstream]
    assert upstream in kb.parent_ids(board, replacement)
    assert kb.get_task(board, replacement).status == "todo"


def test_supersede_refuses_running_replacement_without_changing_graph(board):
    upstream = kb.create_task(board, title="upstream", assignee="worker")
    obsolete = kb.create_task(
        board, title="obsolete", assignee="default", parents=[upstream]
    )
    replacement = kb.create_task(board, title="replacement", assignee="worker")
    child = kb.create_task(
        board, title="child", assignee="worker", parents=[obsolete]
    )
    assert kb.claim_task(board, replacement) is not None

    with pytest.raises(RuntimeError, match="replacement task is running"):
        kb.supersede_task(
            board,
            obsolete,
            reason="duplicate",
            replacement_task_id=replacement,
        )

    assert kb.get_task(board, obsolete).status == "todo"
    assert kb.parent_ids(board, replacement) == []
    assert kb.parent_ids(board, child) == [obsolete]


def test_supersede_handles_unmaterialized_worktree(board):
    task_id = kb.create_task(
        board,
        title="never dispatched",
        assignee="default",
        workspace_kind="worktree",
        workspace_path=None,
    )

    kb.supersede_task(board, task_id, reason="obsolete")

    assert kb.get_task(board, task_id).status == "archived"


def test_move_to_triage_records_reason_without_releasing_children(board):
    parent = kb.create_task(board, title="deferred", assignee="default")
    child = kb.create_task(board, title="child", assignee="worker", parents=[parent])

    assert kb.move_task_to_triage(board, parent, reason="defer until Q4") is True

    assert kb.get_task(board, parent).status == "triage"
    assert kb.get_task(board, child).status == "todo"
    event = kb.list_events(board, parent)[-1]
    assert event.kind == "moved_to_triage"
    assert event.payload == {"from_status": "ready", "reason": "defer until Q4"}


def test_move_to_triage_preserves_real_human_blockers(board):
    task_id = kb.create_task(board, title="needs approval", assignee="default")
    kb.block_task(board, task_id, reason="human approval", kind="needs_input")

    with pytest.raises(RuntimeError, match="blocked"):
        kb.move_task_to_triage(board, task_id, reason="defer")

    assert kb.get_task(board, task_id).status == "blocked"


def test_move_to_triage_audits_repeated_request(board):
    task_id = kb.create_task(board, title="later", assignee="default")
    kb.move_task_to_triage(board, task_id, reason="first deferral")

    kb.move_task_to_triage(board, task_id, reason="still deferred")

    event = kb.list_events(board, task_id)[-1]
    assert event.kind == "moved_to_triage"
    assert event.payload == {"from_status": "triage", "reason": "still deferred"}


def test_move_to_triage_does_not_report_unchanged_status(board, monkeypatch):
    task_id = kb.create_task(board, title="later", assignee="default")
    kb.move_task_to_triage(board, task_id, reason="first deferral")
    updates = []
    monkeypatch.setattr(
        kb,
        "notify_task_updated",
        lambda _conn, tid, fields: updates.append((tid, fields)),
    )

    kb.move_task_to_triage(board, task_id, reason="still deferred")

    assert updates == []


def test_supersede_preserves_real_human_blockers(board):
    task_id = kb.create_task(board, title="needs approval", assignee="default")
    kb.block_task(board, task_id, reason="human approval", kind="needs_input")

    with pytest.raises(RuntimeError, match="human action"):
        kb.supersede_task(board, task_id, reason="cleanup")

    assert kb.get_task(board, task_id).status == "blocked"


def test_supersede_rejects_self_replacement(board):
    obsolete = kb.create_task(board, title="obsolete", assignee="default")
    kb.create_task(board, title="child", assignee="worker", parents=[obsolete])

    with pytest.raises(RuntimeError, match="replace itself"):
        kb.supersede_task(
            board,
            obsolete,
            reason="duplicate",
            replacement_task_id=obsolete,
        )

    assert kb.get_task(board, obsolete).status == "ready"


def test_supersede_does_not_audit_an_existing_replacement_link(board):
    obsolete = kb.create_task(board, title="obsolete", assignee="default")
    replacement = kb.create_task(board, title="replacement", assignee="worker")
    child = kb.create_task(
        board, title="child", assignee="worker", parents=[obsolete, replacement]
    )
    linked_before = len(
        [event for event in kb.list_events(board, child) if event.kind == "linked"]
    )

    result = kb.supersede_task(
        board,
        obsolete,
        reason="duplicate",
        replacement_task_id=replacement,
    )

    assert result["relinked_children"] == []
    linked_after = len(
        [event for event in kb.list_events(board, child) if event.kind == "linked"]
    )
    assert linked_after == linked_before


def test_assign_refuses_terminal_task_inside_db_transaction(board):
    task_id = kb.create_task(board, title="finished", assignee="default")
    kb.complete_task(board, task_id, result="done")

    with pytest.raises(RuntimeError, match="done"):
        kb.assign_task(board, task_id, "reviewer", reason="too late")

    assert kb.get_task(board, task_id).assignee == "default"


def test_assign_preserves_real_human_blocker(board):
    task_id = kb.create_task(board, title="approval", assignee="default")
    kb.block_task(board, task_id, reason="approval", kind="needs_input")

    with pytest.raises(RuntimeError, match="human action"):
        kb.assign_task(board, task_id, "reviewer", reason="route")

    assert kb.get_task(board, task_id).assignee == "default"
