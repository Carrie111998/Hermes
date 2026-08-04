"""Local regression tests for reliable Kanban successors and origin wakes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers import _kanban_origin_wake_handoff
from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    return home


def test_explicit_unknown_project_worktree_is_rejected(isolated_kanban_home):
    conn = kb.connect()
    try:
        with pytest.raises(ValueError, match="project.*does-not-exist.*not found"):
            kb.create_task(
                conn,
                title="repair",
                project_id="does-not-exist",
                workspace_kind="worktree",
            )
    finally:
        conn.close()


def test_headless_successor_inherits_full_notification_lineage(
    isolated_kanban_home, monkeypatch
):
    from tools import kanban_tools

    conn = kb.connect()
    try:
        parent = kb.create_task(
            conn,
            title="audit",
            assignee="default",
            workspace_kind="scratch",
        )
        kb.add_notify_sub(
            conn,
            task_id=parent,
            platform="discord",
            chat_id="origin-thread",
            chat_type="group",
            thread_id="thread-7",
            user_id="atomic",
            notifier_profile="default",
            delivery_metadata={"thread_id": "thread-7", "chat_type": "group"},
        )
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setattr(kanban_tools, "_maybe_auto_subscribe", lambda *_a, **_k: False)

    result = json.loads(
        kanban_tools._handle_create(
            {
                "title": "repair",
                "assignee": "default",
                "parents": [parent],
            }
        )
    )
    assert result["ok"] is True
    assert result["subscribed"] is True

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, result["task_id"])
    finally:
        conn.close()

    assert len(subs) == 1
    sub = subs[0]
    assert sub["platform"] == "discord"
    assert sub["chat_id"] == "origin-thread"
    assert sub["chat_type"] == "group"
    assert sub["thread_id"] == "thread-7"
    assert sub["user_id"] == "atomic"
    assert sub["notifier_profile"] == "default"
    assert sub["delivery_metadata"] == {
        "chat_type": "group",
        "thread_id": "thread-7",
    }


def test_origin_wake_prefers_event_summary_and_preserves_multiline_handoff():
    events = [
        SimpleNamespace(kind="blocked", payload={"summary": "ignore"}),
        SimpleNamespace(
            kind="completed",
            payload={"summary": "artifact ready\n/home/cc/work/result.json"},
        ),
    ]
    task = SimpleNamespace(result="older task result")

    handoff = _kanban_origin_wake_handoff(events, task)

    assert handoff == "artifact ready\n/home/cc/work/result.json"


def test_origin_wake_handoff_falls_back_to_task_and_marks_truncation():
    task = SimpleNamespace(result="x" * 1300)
    handoff = _kanban_origin_wake_handoff([], task, max_chars=1200)

    assert len(handoff) == 1200
    assert handoff.endswith("…")


def test_push_handoff_preserves_multiline_summary_and_marks_truncation():
    from gateway.kanban_watchers import _kanban_completion_handoff

    summary = "artifact ready\n/home/cc/work/result.json"
    assert _kanban_completion_handoff(summary) == "\n" + summary
    rendered = _kanban_completion_handoff("x" * 2000)
    assert len(rendered) == 1501
    assert rendered.endswith("…")
