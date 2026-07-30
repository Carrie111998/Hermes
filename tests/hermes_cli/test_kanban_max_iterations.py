"""Per-card Kanban worker iteration budgets.

Covers persistence/migration, CLI/tool creation surfaces, and dispatcher
propagation to the worker's explicit ``--max-turns`` runtime override.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_max_iterations_defaults_to_profile_budget(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.max_iterations is None


def test_max_iterations_persists_and_is_audited(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded",
            assignee="worker",
            max_iterations=45,
        )
        task = kb.get_task(conn, tid)
        created = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
    assert task.max_iterations == 45
    assert created.payload["max_iterations"] == 45


@pytest.mark.parametrize("value", [0, -1])
def test_max_iterations_must_be_positive(kanban_home, value):
    with kb.connect() as conn, pytest.raises(ValueError, match="max_iterations must be >= 1"):
        kb.create_task(conn, title="bad", max_iterations=value)


def test_legacy_db_migrates_max_iterations_column(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        task = kb.get_task(conn, "legacy1")
    assert "max_iterations" in cols
    assert task.max_iterations is None


def _spawn_and_capture(monkeypatch, kanban_home, task):
    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    kb._default_spawn(task, str(kanban_home))
    return captured


def test_spawn_passes_explicit_worker_budget(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded",
            assignee="default",
            max_iterations=45,
        )
        task = kb.get_task(conn, tid)
    cmd = _spawn_and_capture(monkeypatch, kanban_home, task)["cmd"]
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "45"
    assert cmd.index("chat") < idx


def test_spawn_uses_profile_budget_when_no_override(kanban_home, monkeypatch):
    profile_home = kanban_home / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    profile_home.joinpath("config.yaml").write_text(
        "agent:\n  max_turns: 47\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        task = kb.get_task(conn, tid)
    captured = _spawn_and_capture(monkeypatch, kanban_home, task)
    assert "--max-turns" not in captured["cmd"]
    assert captured["env"]["HERMES_HOME"] == str(profile_home)

    from hermes_cli.config import load_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profile_home))
    try:
        assert load_config()["agent"]["max_turns"] == 47
    finally:
        reset_hermes_home_override(token)


def test_cli_create_accepts_max_iterations(kanban_home):
    out = kc.run_slash(
        "create 'bounded cli' --assignee worker --max-iterations 37 --json"
    )
    payload = json.loads(out)
    assert payload["max_iterations"] == 37
    with kb.connect() as conn:
        assert kb.get_task(conn, payload["id"]).max_iterations == 37


@pytest.mark.parametrize("value", [0, -1])
def test_cli_create_rejects_non_positive_max_iterations(kanban_home, value):
    out = kc.run_slash(
        f"create 'bad cli budget' --assignee worker --max-iterations {value} --json"
    )
    assert "--max-iterations must be >= 1" in out
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_tool_create_accepts_max_iterations(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt

    assert "max_iterations" in kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    result = json.loads(
        kt._handle_create(
            {
                "title": "bounded child",
                "assignee": "worker",
                "max_iterations": 29,
            }
        )
    )
    assert result["ok"] is True
    assert result["max_iterations"] == 29
    with kb.connect() as conn:
        assert kb.get_task(conn, result["task_id"]).max_iterations == 29
    shown = json.loads(kt._handle_show({"task_id": result["task_id"]}))
    assert shown["task"]["max_iterations"] == 29


@pytest.mark.parametrize("value", [0, -1])
def test_tool_create_rejects_non_positive_max_iterations(
    kanban_home, monkeypatch, value
):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt

    result = json.loads(
        kt._handle_create(
            {
                "title": "bad tool budget",
                "assignee": "worker",
                "max_iterations": value,
            }
        )
    )
    assert "error" in result
    assert "max_iterations must be >= 1" in result["error"]
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []
