"""Spec 042 §5 phase A2 — ``workflow_ref`` / ``workflow_args`` columns + CLI.

Covers the additive ``tasks.workflow_ref`` / ``tasks.workflow_args``
migration (fresh schema + legacy-board backfill), ``Task.from_row``
round-trip, ``create_task`` validation (args require a ref, args must be
a JSON object, canonical sorted-key storage), and the
``kanban create --workflow/--args`` / ``kanban show`` surface. The
resolver that turns ``workflow_ref`` into the harness-native invocation
is a separate phase and out of scope here.
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
    (home / "profiles" / "elias").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _legacy_db_without_workflow_columns(path: Path) -> None:
    """Write a kanban DB whose ``tasks`` table predates the workflow
    columns (and spec 042 generally)."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            spawn_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_spawn_error TEXT
        )
    """)
    # task_events is required: _migrate_add_optional_columns also runs a
    # PRAGMA on it to back-fill the run_id column and raises
    # OperationalError if the table is absent.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old card', 'ready', 1)"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_workflow_columns_exist_on_fresh_db(kanban_home):
    with kb.connect_closing() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "workflow_ref" in cols
    assert "workflow_args" in cols


def test_workflow_columns_migrated_onto_legacy_board(tmp_path):
    """Legacy boards gain the workflow columns as NULL — existing cards
    keep their exact pre-spec behaviour (no workflow binding)."""
    db_path = tmp_path / "legacy.db"
    _legacy_db_without_workflow_columns(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    kb._migrate_add_optional_columns(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col in ("workflow_ref", "workflow_args"):
        assert col in cols, f"migration must add tasks.{col}"

    row = conn.execute("SELECT * FROM tasks WHERE id = 'legacy'").fetchone()
    task = kb.Task.from_row(row)
    assert task.workflow_ref is None
    assert task.workflow_args is None

    # Idempotent second run must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_workflow_round_trip(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="workflow card",
            workflow_ref="sppcrt",
            workflow_args='{"b": 2, "a": 1}',
        )
        task = kb.get_task(conn, task_id)
        assert task.workflow_ref == "sppcrt"
        # Stored canonicalised: sorted keys, so semantically identical
        # input round-trips byte-identically.
        assert task.workflow_args == '{"a": 1, "b": 2}'
        assert json.loads(task.workflow_args) == {"a": 1, "b": 2}

        events = kb.list_events(conn, task_id)
        created = [e for e in events if e.kind == "created"]
        assert created, "create_task must append a created event"
        assert created[0].payload["workflow_ref"] == "sppcrt"
        assert created[0].payload["workflow_args"] == '{"a": 1, "b": 2}'


def test_create_task_workflow_defaults_to_none(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="plain card")
        task = kb.get_task(conn, task_id)
        assert task.workflow_ref is None
        assert task.workflow_args is None


def test_create_task_workflow_args_requires_ref(kanban_home):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="requires a workflow_ref"):
            kb.create_task(conn, title="bad", workflow_args='{"a": 1}')


def test_create_task_workflow_args_must_be_json_object(kanban_home):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="must be a JSON object"):
            kb.create_task(conn, title="bad", workflow_ref="x",
                           workflow_args="not json")
        with pytest.raises(ValueError, match="must be a JSON object"):
            kb.create_task(conn, title="bad", workflow_ref="x",
                           workflow_args='["a", "b"]')


def test_create_task_workflow_ref_normalised(kanban_home):
    """Blank/whitespace refs and args normalise to None (no binding)."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="blank", workflow_ref="  ", workflow_args="",
        )
        task = kb.get_task(conn, task_id)
        assert task.workflow_ref is None
        assert task.workflow_args is None


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_create_show_round_trip_workflow_fields(kanban_home):
    """`kanban create --workflow/--args` persists the binding and both
    `kanban show` modes expose it."""
    out = kc.run_slash(
        "create \"workflow card\" --workflow sppcrt --args '{\"a\":1}' --json"
    )
    created = json.loads(out)
    assert created["workflow_ref"] == "sppcrt"
    assert created["workflow_args"] == '{"a": 1}'
    # CLI-filed execution pins stamp routed_by=operator.
    assert created["routed_by"] == "operator"

    shown = json.loads(kc.run_slash(f"show {created['id']} --json"))
    assert shown["task"]["workflow_ref"] == "sppcrt"
    assert shown["task"]["workflow_args"] == '{"a": 1}'

    text = kc.run_slash(f"show {created['id']}")
    assert "workflow:  sppcrt" in text
    assert 'workflow-args: {"a": 1}' in text


def test_cli_create_workflow_alone_still_pins_routing(kanban_home):
    out = kc.run_slash('create "wf only" --workflow vault-morning --json')
    created = json.loads(out)
    assert created["workflow_ref"] == "vault-morning"
    assert created["workflow_args"] is None
    assert created["routed_by"] == "operator"


def test_cli_create_args_without_workflow_fails(kanban_home):
    out = kc.run_slash("create \"bad\" --args '{\"a\":1}'")
    assert "workflow_args requires a workflow_ref" in out


def test_cli_create_args_must_be_json_object(kanban_home):
    out = kc.run_slash('create "bad" --workflow x --args not-json')
    assert "workflow_args must be a JSON object" in out
