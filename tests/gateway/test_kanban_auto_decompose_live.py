"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from gateway.kanban_watchers import _resolve_auto_decompose_settings


def test_enabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is True
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


@pytest.mark.asyncio
async def test_gateway_auto_decompose_preserves_scoped_project_snapshot(tmp_path, monkeypatch):
    """The embedded gateway path must use board metadata, not local projects.db."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()

    repo = home / "repo"
    repo.mkdir()
    with pdb.connect_closing() as conn:
        project_id = pdb.create_project(conn, name="Gateway Project", primary_path=str(repo))
        project = pdb.get_project(conn, project_id)
    assert project is not None
    kb.create_board("scoped", project_id=project.id, legacy_unscoped=False)
    kb.set_current_board("scoped")
    assert kb.kanban_db_path("scoped").resolve().is_relative_to(home.resolve())
    with kb.connect(board="scoped") as conn:
        root_id = kb.create_task(conn, title="gateway scoped root", triage=True, board="scoped")
    pdb.projects_db_path().unlink()

    payload = (
        '{"fanout": true, "rationale": "gateway test", "tasks": ['
        '{"title": "research", "body": "inspect", "assignee": "researcher", "parents": []},'
        '{"title": "build", "body": "implement", "assignee": "engineer", "parents": [0]}]}'
    )
    profiles = [
        SimpleNamespace(name=name, description=f"{name} profile")
        for name in ("orchestrator", "researcher", "engineer")
    ]
    config = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": True,
            "auto_decompose_per_tick": 1,
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda: profiles)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {p.name for p in profiles})
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "orchestrator")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda *args, **kwargs: response)

    monkeypatch.setattr(kanban_watchers, "_acquire_singleton_lock", lambda path: (None, "unavailable"))
    monkeypatch.setattr(kanban_watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [kb.read_board_metadata("scoped")])
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(
        kb,
        "dispatch_once",
        lambda *args, **kwargs: SimpleNamespace(
            spawned=[], reclaimed=0, crashed=[], timed_out=[], promoted=0, auto_blocked=[]
        ),
    )

    runner = object.__new__(GatewayRunner)
    runner._running = True
    calls = []

    async def immediate_sleep(_seconds):
        return None

    async def run_in_thread(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", "callable"))
        result = fn(*args, **kwargs)
        if len(calls) >= 4:
            runner._running = False
        return result

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", immediate_sleep)
    monkeypatch.setattr(kanban_watchers.asyncio, "to_thread", run_in_thread)
    await runner._kanban_dispatcher_watcher()

    assert "_auto_decompose_tick" in calls
    assert not any(key.startswith("HERMES_KANBAN_") for key in os.environ)
    with kb.connect(board="scoped") as conn:
        rows = conn.execute(
            "SELECT project_id, legacy_unscoped FROM tasks "
            "WHERE id != ? ORDER BY created_at, id",
            (root_id,),
        ).fetchall()
    assert len(rows) == 2
    assert [row["project_id"] for row in rows] == [project.id, project.id]
    assert [bool(row["legacy_unscoped"]) for row in rows] == [False, False]


