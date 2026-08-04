from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from tools import delegate_tool
from tools import kanban_tools


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.init_db()
    connection = kb.connect()
    yield connection
    connection.close()


def _task(conn: sqlite3.Connection) -> str:
    return kb.create_task(conn, title="owner task", assignee="worker")


def test_delegate_run_lifecycle_is_bounded_execution_metadata(conn):
    task_id = _task(conn)
    run = kb.create_delegate_run(
        conn,
        task_id=task_id,
        delegate_id="sub_abc",
        goal="inspect implementation",
        role="leaf",
        route="luna",
        model="gpt-test",
        artifact_path="/tmp/report.json",
    )
    assert run.status == "queued"
    assert not hasattr(run, "transcript")
    assert not hasattr(run, "logs")
    assert not hasattr(run, "acceptance")

    running = kb.start_delegate_run(conn, run.id)
    assert running.status == "running"
    assert running.started_at is not None

    finished = kb.finish_delegate_run(
        conn,
        run.id,
        status="done",
        summary="inspection complete",
        artifact_path="/tmp/report.json",
        commit_sha="abc123",
        verification="3 tests passed",
    )
    assert finished.status == "done"
    assert finished.summary == "inspection complete"
    assert finished.ended_at is not None
    assert [item.id for item in kb.list_delegate_runs(conn, task_id)] == [run.id]


def test_delegate_run_rejects_unknown_parent_and_invalid_state(conn):
    with pytest.raises(ValueError, match="parent task"):
        kb.create_delegate_run(
            conn,
            task_id="missing",
            delegate_id="sub_missing",
            goal="no parent",
        )

    task_id = _task(conn)
    run = kb.create_delegate_run(
        conn,
        task_id=task_id,
        delegate_id="sub_validation",
        goal="validate state",
    )
    with pytest.raises(ValueError, match="status"):
        kb.finish_delegate_run(conn, run.id, status="mystery")


def test_delegate_tool_automatically_records_bounded_kanban_metadata(conn, monkeypatch):
    task_id = _task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    child = SimpleNamespace(
        _subagent_id="sub_auto",
        _delegate_role="leaf",
        _delegate_route="luna",
        model="gpt-test",
    )

    run_id = delegate_tool._start_kanban_delegate_run(child, "inspect safely")
    assert run_id is not None
    delegate_tool._finish_kanban_delegate_run(
        run_id,
        {
            "status": "completed",
            "summary": "x" * 5000,
            "artifact_path": "/stable/report.md",
            "commit_sha": "deadbeef",
            "verification": "pytest passed",
            "messages": ["must not be stored"],
            "tool_trace": [{"large": "must not be stored"}],
        },
    )

    saved = kb.get_delegate_run(conn, run_id)
    assert saved is not None
    assert saved.status == "done"
    assert saved.route == "luna"
    assert saved.artifact_path == "/stable/report.md"
    assert saved.commit_sha == "deadbeef"
    assert saved.verification == "pytest passed"
    assert len(saved.summary or "") == 4096
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delegate_runs)").fetchall()
    }
    assert not {
        "messages",
        "tool_trace",
        "transcript",
        "logs",
        "acceptance",
        "reviewed_at",
    } & columns


def test_delegate_tool_serializes_structured_verification(conn, monkeypatch):
    task_id = _task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    child = SimpleNamespace(
        _subagent_id="sub_structured",
        _delegate_role="leaf",
        _delegate_route="luna",
        model="gpt-test",
    )

    run_id = delegate_tool._start_kanban_delegate_run(child, "verify output")
    assert run_id is not None
    delegate_tool._finish_kanban_delegate_run(
        run_id,
        {
            "status": "completed",
            "summary": "done",
            "verification": {"passed": True, "tests": ["pytest", "ruff"]},
        },
    )

    saved = kb.get_delegate_run(conn, run_id)
    assert saved is not None
    assert saved.status == "done"
    assert json.loads(saved.verification or "{}") == {
        "passed": True,
        "tests": ["pytest", "ruff"],
    }


def test_kanban_show_surfaces_delegate_execution_ledger(conn, monkeypatch):
    task_id = _task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    run = kb.create_delegate_run(
        conn, task_id=task_id, delegate_id="sub_review", goal="show me"
    )
    kb.start_delegate_run(conn, run.id)
    kb.finish_delegate_run(conn, run.id, status="done", summary="ready")

    shown = json.loads(kanban_tools._handle_show({"task_id": task_id}))
    item = shown["delegate_runs"][0]
    assert item["delegate_id"] == "sub_review"
    assert item["summary"] == "ready"
    assert "acceptance" not in item
    assert "reviewed_at" not in item


def test_legacy_acceptance_columns_are_ignored(conn):
    conn.execute(
        "ALTER TABLE delegate_runs ADD COLUMN acceptance TEXT NOT NULL DEFAULT 'pending'"
    )
    conn.execute("ALTER TABLE delegate_runs ADD COLUMN reviewed_at INTEGER")
    conn.commit()
    task_id = _task(conn)
    run = kb.create_delegate_run(
        conn, task_id=task_id, delegate_id="sub_legacy", goal="compat"
    )
    assert kb.get_delegate_run(conn, run.id) is not None


def test_delegate_review_tool_is_not_exposed(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")

    import tools.kanban_tools  # noqa: F401 - registration side effect
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {item["function"]["name"] for item in schema if "function" in item}
    assert "kanban_delegate_review" not in names
    assert "kanban_list" not in names
