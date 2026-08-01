"""Class-of-Service metadata stays validated, audited, and inert."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    connection = kb.connect()
    yield connection
    connection.close()


def _load_plugin_router():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_class_of_service_test", plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_contract_authority_is_shared_across_tool_and_api():
    values = list(kb.VALID_CLASSES_OF_SERVICE)
    assert kanban_tools.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["class_of_service"]["enum"] == values


def test_values_create_validate_and_preserve_rollback(conn):
    for value in kb.VALID_CLASSES_OF_SERVICE:
        task_id = kb.create_task(conn, title=value, class_of_service=value)
        assert kb.get_task(conn, task_id).class_of_service == value

    before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    with pytest.raises(ValueError):
        kb.create_task(conn, title="invalid", class_of_service="rush")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events


def test_legacy_persistence_migrates_as_unclassified(tmp_path):
    path = tmp_path / "legacy.db"
    schema_lines = kb.SCHEMA_SQL.splitlines(keepends=True)
    class_of_service_lines = [
        index
        for index, line in enumerate(schema_lines)
        if line.strip().split(maxsplit=1)[:1] == ["class_of_service"]
    ]
    assert len(class_of_service_lines) == 1
    legacy_schema = "".join(
        line for index, line in enumerate(schema_lines) if index != class_of_service_lines[0]
    )
    raw = sqlite3.connect(path)
    raw.executescript(legacy_schema)
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at, workspace_kind) VALUES (?, ?, ?, ?, ?)",
        ("legacy", "legacy task", "ready", 1, "scratch"),
    )
    raw.commit()
    assert "class_of_service" not in {
        row[1] for row in raw.execute("PRAGMA table_info(tasks)")
    }
    raw.close()

    with kb.connect(db_path=path) as connection:
        assert kb.get_task(connection, "legacy").class_of_service is None
        assert "class_of_service" in {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}


def test_update_is_audited_idempotent_and_clearable(conn):
    task_id = kb.create_task(conn, title="metadata")
    assert kb.set_class_of_service(conn, task_id, "expedite")
    assert kb.set_class_of_service(conn, task_id, "expedite")
    assert kb.set_class_of_service(conn, task_id, None)
    assert kb.get_task(conn, task_id).class_of_service is None
    events = [event for event in kb.list_events(conn, task_id) if event.kind == "class_of_service_set"]
    assert [event.payload["class_of_service"] for event in events] == ["expedite", None]
    assert [event.payload["previous_class_of_service"] for event in events] == [None, "expedite"]


def test_cli_create_set_json_and_show(kanban_home):
    created = json.loads(kc.run_slash("create 'CLI task' --class-of-service standard --json"))
    assert created["class_of_service"] == "standard"
    task_id = created["id"]
    cleared = json.loads(kc.run_slash(f"set-class-of-service {task_id} none --json"))
    assert cleared["class_of_service"] is None
    assert "class-of-service" not in kc.run_slash(f"show {task_id}")


def test_tool_schema_and_summary_round_trip(kanban_home):
    result = json.loads(kanban_tools._handle_create({
        "title": "Tool task", "assignee": "worker", "class_of_service": "fixed_date",
    }))
    assert result["ok"] is True
    with kb.connect() as connection:
        task = kb.get_task(connection, result["task_id"])
        assert kanban_tools._task_summary_dict(kb, connection, task)["class_of_service"] == "fixed_date"


def test_api_create_patch_clear_invalid_and_board_options(client):
    created = client.post("/api/plugins/kanban/tasks", json={
        "title": "API task", "assignee": "worker", "priority": 2, "class_of_service": "intangible",
    })
    assert created.status_code == 200, created.text
    task = created.json()["task"]
    assert task["class_of_service"] == "intangible"

    updated = client.patch(f"/api/plugins/kanban/tasks/{task['id']}", json={
        "class_of_service": "standard", "priority": 4,
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["task"]["priority"] == 4
    assert updated.json()["task"]["class_of_service"] == "standard"

    omitted = client.patch(f"/api/plugins/kanban/tasks/{task['id']}", json={"priority": 5})
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["task"]["class_of_service"] == "standard"

    rejected = client.patch(f"/api/plugins/kanban/tasks/{task['id']}", json={
        "class_of_service": "rush", "priority": 9,
    })
    assert rejected.status_code == 400
    assert client.get(f"/api/plugins/kanban/tasks/{task['id']}").json()["task"]["priority"] == 5

    cleared = client.patch(f"/api/plugins/kanban/tasks/{task['id']}", json={"class_of_service": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["task"]["class_of_service"] is None
    assert client.get("/api/plugins/kanban/board").json()["classes_of_service"] == list(kb.VALID_CLASSES_OF_SERVICE)


def test_ordering_and_dispatch_ignore_class_of_service(conn, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    expedite = kb.create_task(conn, title="expedite", assignee="default", priority=1, class_of_service="expedite")
    standard = kb.create_task(conn, title="standard", assignee="default", priority=9, class_of_service="standard")
    task_ids = [task.id for task in kb.list_tasks(conn)]
    assert task_ids.index(standard) < task_ids.index(expedite)
    assert kb.get_task(conn, expedite).status == "ready"
    assert kb.get_task(conn, standard).status == "ready"

    before = {
        task_id: (kb.get_task(conn, task_id).status, kb.get_task(conn, task_id).consecutive_failures)
        for task_id in (expedite, standard)
    }
    result = kb.dispatch_once(
        conn, spawn_fn=lambda *_args, **_kwargs: 123, dry_run=True, max_spawn=8
    )
    assert [item[0] for item in result.spawned] == [standard, expedite]
    assert {
        task_id: (kb.get_task(conn, task_id).status, kb.get_task(conn, task_id).consecutive_failures)
        for task_id in (expedite, standard)
    } == before
