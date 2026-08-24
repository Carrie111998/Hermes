"""Undeclared scratch-workspace content must not vanish silently (#93164)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as the
    fixture local to test_kanban_db.py, which a sibling file cannot see)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _scratch_task_with_file(kanban_home, tmp_path, name, size):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="undeclared content task")
        t = kb.get_task(conn, tid)
        ws = kb.resolve_workspace(t)
        kb.set_workspace_path(conn, tid, ws)
        ws.mkdir(parents=True, exist_ok=True)
        if size:
            (ws / name).write_text("x" * size)
        return tid, ws


def test_undeclared_content_warns_and_events(kanban_home, tmp_path, caplog):
    tid, ws = _scratch_task_with_file(kanban_home, tmp_path, "report.md", 2048)
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        with kb.connect() as conn:
            kb._cleanup_workspace(conn, tid)
    assert "report.md" in caplog.text
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'workspace_discarded_content'"
        ).fetchall()
    assert rows and "report.md" in rows[0][0]


def test_trivial_content_stays_silent(kanban_home, tmp_path, caplog):
    tid, ws = _scratch_task_with_file(kanban_home, tmp_path, ".keep", 8)
    with kb.connect() as conn:
        kb._cleanup_workspace(conn, tid)
    assert "workspace_discarded_content" not in caplog.text
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE kind = 'workspace_discarded_content'"
        ).fetchone() is None


def test_signal_never_blocks_cleanup(kanban_home, tmp_path):
    tid, ws = _scratch_task_with_file(kanban_home, tmp_path, "big.bin", 4096)
    with kb.connect() as conn:
        kb._cleanup_workspace(conn, tid)
    assert not ws.exists(), "best-effort semantics unchanged: dir still removed"


def test_many_files_event_bounded_and_counts_accurate(kanban_home, tmp_path, caplog):
    """11+ undeclared files: event lists 10 names, counts stay exact (#93164)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="many files")
        ws = kb.resolve_workspace(kb.get_task(conn, tid))
        kb.set_workspace_path(conn, tid, ws)
        ws.mkdir(parents=True, exist_ok=True)  # resolve_workspace may already have
        for i in range(12):
            (ws / f"f{i:02}.dat").write_text("z" * 100)
    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        with kb.connect() as conn:
            kb._cleanup_workspace(conn, tid)
    assert "f00.dat" in caplog.text and "f09.dat" in caplog.text
    assert "f10.dat" not in caplog.text and "+2 more" in caplog.text
    with kb.connect() as conn:
        payload = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'workspace_discarded_content'"
        ).fetchone()
    assert payload
    body = json.loads(payload[0])
    assert body["file_count"] == 12 and body["total_bytes"] == 1200
    assert len(body["files"]) == 10


def test_deferred_parent_cleanup_also_signals(kanban_home, tmp_path, caplog):
    """The deferred parent-sweep rmtree must emit the same signal (#93164):
    a parent whose cleanup waited on children would otherwise destroy its
    undeclared files silently on the second rmtree path."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(conn, title="dir child",
                               workspace_kind="dir", workspace_path=str(tmp_path / "c"))
        (tmp_path / "c").mkdir()
        kb.link_tasks(conn, parent, child)
        ws = kb.resolve_workspace(kb.get_task(conn, parent))
        kb.set_workspace_path(conn, parent, ws)
        (ws / "undeclared.md").write_text("q" * 512)  # resolve_workspace made the dir

    with caplog.at_level(logging.WARNING, logger=kb._log.name):
        kb.complete_task(conn, parent, result="handoff")  # deferred: child active
        assert "undeclared.md" not in caplog.text
        kb.complete_task(conn, child, result="built")     # triggers parent sweep

    assert "undeclared.md" in caplog.text
    with kb.connect() as conn:
        payloads = [p for (p,) in conn.execute(
            "SELECT payload FROM task_events"
            " WHERE kind = 'workspace_discarded_content'"
        ).fetchall() if p]
    assert any("undeclared.md" in p for p in payloads)
