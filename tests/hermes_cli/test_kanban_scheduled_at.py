"""Regression tests for #80119: kanban_create silently dropped scheduled_at.

The tool schema lacked ``scheduled_at``, so an orchestrator agent calling
``kanban_create(..., scheduled_at=\"2026-06-01T03:00:00Z\")`` had the value
silently ignored — the task was created and dispatched immediately, with no
error. The DB layer also had no ``scheduled_at`` column at all, so the
documented delayed-dispatch feature was unreachable from the tool.

These tests pin the full chain: schema column, create_task storage,
dispatcher gating (future = skip, past = dispatch), and legacy-DB migration.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Spin up a fresh HERMES_HOME with kanban DB + alpha/beta profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_scheduled_at_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db

    yield kanban_db


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


def test_create_task_stores_scheduled_at(isolated_kanban_home_with_profiles):
    """scheduled_at must round-trip through create_task into the row."""
    kbd = isolated_kanban_home_with_profiles
    future = int(time.time()) + 3600
    with kbd.connect_closing() as conn:
        tid = kbd.create_task(
            conn, title="delayed", assignee="alpha", scheduled_at=future
        )
        task = kbd.get_task(conn, tid)
        assert task.scheduled_at == future
        # No scheduled_at -> NULL (immediate dispatch preserved).
        tid2 = kbd.create_task(conn, title="immediate", assignee="alpha")
        assert kbd.get_task(conn, tid2).scheduled_at is None


def test_dispatcher_skips_future_scheduled_at(isolated_kanban_home_with_profiles):
    """A ready task whose scheduled_at is in the future must NOT spawn."""
    kbd = isolated_kanban_home_with_profiles
    with kbd.connect_closing() as conn:
        kbd.create_board(slug="default", name="Test")
        tid = kbd.create_task(
            conn,
            title="future job",
            assignee="alpha",
            scheduled_at=int(time.time()) + 3600,
        )
    with kbd.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    spawned_ids = [s[0] for s in res.spawned]
    assert tid not in spawned_ids, (
        "future-scheduled task was dispatched before its scheduled_at"
    )


def test_dispatcher_dispatchs_past_scheduled_at(isolated_kanban_home_with_profiles):
    """A ready task whose scheduled_at is in the past MUST spawn."""
    kbd = isolated_kanban_home_with_profiles
    with kbd.connect_closing() as conn:
        kbd.create_board(slug="default", name="Test")
        tid = kbd.create_task(
            conn,
            title="overdue job",
            assignee="alpha",
            scheduled_at=int(time.time()) - 60,
        )
    with kbd.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    spawned_ids = [s[0] for s in res.spawned]
    assert tid in spawned_ids, (
        "past-scheduled task was not dispatched when its time arrived"
    )


def test_scheduled_at_column_exists_in_schema(kanban_home):
    """Fresh DBs must include the scheduled_at column."""
    with kb.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "scheduled_at" in cols


def test_scheduled_at_migrated_on_legacy_db(tmp_path, monkeypatch):
    """Opening a legacy DB (no scheduled_at) must add the column."""
    legacy = tmp_path / "legacy.db"
    # Build a pre-#80119 schema by creating a fresh DB, then dropping the
    # scheduled_at column via table rebuild.
    conn = kb.connect(db_path=legacy)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "scheduled_at" in cols  # fresh schema has it
    conn.execute("CREATE TABLE tasks_old AS SELECT * FROM tasks")
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_old RENAME TO tasks")
    conn.commit()
    conn.close()

    # Reopen via connect() -> init_db -> _migrate_add_optional_columns.
    conn = kb.connect(db_path=legacy)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "scheduled_at" in cols
    finally:
        conn.close()
