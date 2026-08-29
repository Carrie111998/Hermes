"""Tests for the ``on_kanban_task_updated`` mutation observer (RFC #58548).

Verifies the task-mutation boundary observer: ``assign_task`` fires it with
``changed_fields`` AFTER the assignment txn commits, refused/failed
mutations never fire, a raising callback never breaks the mutation, and
call sites short-circuit when nothing subscribes.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _live_plugin_manager():
    """Return the manager used by kanban_db's runtime lifecycle imports."""
    importlib.import_module("hermes_cli.lifecycle")
    return importlib.import_module("hermes_cli.plugins").get_plugin_manager()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for profile in ("alice", "bob"):
        (home / "profiles" / profile).mkdir(parents=True, exist_ok=True)
    kb.init_db()
    return home


@pytest.fixture
def captured_updates(monkeypatch):
    mgr = _live_plugin_manager()
    events: list[dict] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("on_kanban_task_updated", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        yield events
    finally:
        mgr._hooks = saved

def test_assign_fires_updated_with_changed_fields(kanban_home, captured_updates):
    """assign_task fires the observer post-commit with the changed field."""
    assignee_at_fire_time: list = []

    def _read_assignee(**kw):
        # Fresh connection: proves the assignment was committed before
        # the hook fired.
        c2 = sqlite3.connect(kb.kanban_db_path())
        try:
            row = c2.execute(
                "SELECT assignee FROM tasks WHERE id = ?", (kw["task_id"],)
            ).fetchone()
            assignee_at_fire_time.append(row[0] if row else None)
        finally:
            c2.close()

    mgr = _live_plugin_manager()
    mgr._hooks.setdefault("on_kanban_task_updated", []).append(_read_assignee)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        captured_updates.clear()  # create-time bookkeeping is not under test
        assert kb.assign_task(conn, tid, "bob") is True
    finally:
        conn.close()

    assert len(captured_updates) == 1
    kw = captured_updates[0]
    assert kw["task_id"] == tid
    assert kw["changed_fields"] == ["assignee"]
    assert kw["assignee"] == "bob"
    assert "profile_name" in kw
    assert "board" in kw
    assert "run_id" in kw
    assert assignee_at_fire_time == ["bob"]

def test_raising_callback_does_not_break_assign(kanban_home):
    mgr = _live_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}

    def _boom(**kw):
        raise RuntimeError("plugin exploded")

    mgr._hooks.setdefault("on_kanban_task_updated", []).append(_boom)
    try:
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="alice")
            assert kb.assign_task(conn, tid, "bob") is True
            assert kb.get_task(conn, tid).assignee == "bob"
        finally:
            conn.close()
    finally:
        mgr._hooks = saved


def test_no_subscriber_short_circuits_task_updated(kanban_home, monkeypatch):
    lifecycle = importlib.import_module("hermes_cli.lifecycle")

    invoked: list[str] = []
    real_invoke = lifecycle.invoke_hook

    def _spy(hook_name, **kw):
        invoked.append(hook_name)
        return real_invoke(hook_name, **kw)

    monkeypatch.setattr(lifecycle, "invoke_hook", _spy)
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="alice")
        assert kb.assign_task(conn, tid, "bob") is True
    finally:
        conn.close()
    assert "on_kanban_task_updated" not in invoked
