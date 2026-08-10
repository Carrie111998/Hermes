"""Regression coverage for Kanban dispatcher respawn guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty Kanban database."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _blocked_task_with_historical_pr(conn, monkeypatch, *, now: int) -> str:
    monkeypatch.setattr(kb.time, "time", lambda: now - 120)
    task_id = kb.create_task(
        conn,
        title="resume after dependency",
        assignee="reader",
        initial_status="blocked",
    )
    kb.add_comment(
        conn,
        task_id,
        "worker",
        "Merged PR https://github.com/NousResearch/hermes-agent/pull/42",
    )
    monkeypatch.setattr(kb.time, "time", lambda: now - 60)
    assert kb.unblock_task(conn, task_id)
    monkeypatch.setattr(kb.time, "time", lambda: now)
    return task_id


def test_explicit_unblock_bypasses_historical_pr_comment(kanban_home, monkeypatch):
    """An operator unblock is a deliberate rerun even when a recent comment
    links a PR produced by an earlier run.
    """
    with kb.connect() as conn:
        task_id = _blocked_task_with_historical_pr(conn, monkeypatch, now=2_000_000)

        assert kb.check_respawn_guard(conn, task_id) is None


def test_recent_pr_comment_still_blocks_ordinary_duplicate_spawn(
    kanban_home, monkeypatch
):
    """Without a later explicit rerun request, a recent PR still guards the task."""
    now = 2_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now - 60)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="avoid duplicate", assignee="reader")
        kb.add_comment(
            conn,
            task_id,
            "worker",
            "Opened https://github.com/NousResearch/hermes-agent/pull/99",
        )
        monkeypatch.setattr(kb.time, "time", lambda: now)

        assert kb.check_respawn_guard(conn, task_id) == "active_pr"


def test_automatic_reclaim_does_not_bypass_recent_pr_comment(kanban_home, monkeypatch):
    """Automatic recovery is not an operator request to duplicate prior PR work."""
    now = 2_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now - 120)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="reclaimed", assignee="reader")
        kb.add_comment(
            conn,
            task_id,
            "worker",
            "Opened https://github.com/NousResearch/hermes-agent/pull/100",
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'reclaimed', ?)",
            (task_id, now - 60),
        )
        monkeypatch.setattr(kb.time, "time", lambda: now)

        assert kb.check_respawn_guard(conn, task_id) == "active_pr"


def test_manual_promotion_bypasses_historical_pr_comment(kanban_home, monkeypatch):
    """The audited manual-promotion event is an explicit rerun request."""
    now = 2_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now - 120)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="promote deliberately",
            assignee="reader",
            initial_status="blocked",
        )
        kb.add_comment(
            conn,
            task_id,
            "worker",
            "Merged https://github.com/NousResearch/hermes-agent/pull/101",
        )
        monkeypatch.setattr(kb.time, "time", lambda: now - 60)
        assert kb.promote_task(conn, task_id, actor="operator") == (True, None)
        monkeypatch.setattr(kb.time, "time", lambda: now)

        assert kb.check_respawn_guard(conn, task_id) is None


def test_same_second_pr_comment_fails_closed_after_unblock(kanban_home, monkeypatch):
    """Second-resolution timestamps must not let a later comment look historical."""
    now = 2_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now - 60)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="same-second ordering",
            assignee="reader",
            initial_status="blocked",
        )
        assert kb.unblock_task(conn, task_id)
        kb.add_comment(
            conn,
            task_id,
            "worker",
            "Opened https://github.com/NousResearch/hermes-agent/pull/102",
        )
        monkeypatch.setattr(kb.time, "time", lambda: now)

        assert kb.check_respawn_guard(conn, task_id) == "active_pr"


def test_dispatch_spawns_unblocked_task_instead_of_leaving_it_ready(
    kanban_home, monkeypatch, all_assignees_spawnable
):
    """A historical PR comment cannot leave explicitly unblocked work ready forever."""
    spawn_calls: list[str] = []

    def spawn(task, _workspace):
        spawn_calls.append(task.id)
        return 4242

    with kb.connect() as conn:
        task_id = _blocked_task_with_historical_pr(conn, monkeypatch, now=2_000_000)

        result = kb.dispatch_once(conn, spawn_fn=spawn)

        assert result.respawn_guarded == []
        assert [task[0] for task in result.spawned] == [task_id]
        assert spawn_calls == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
