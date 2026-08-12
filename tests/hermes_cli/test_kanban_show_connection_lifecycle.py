"""Regression tests for the kanban show connection lifecycle."""

from __future__ import annotations

import argparse
from pathlib import Path

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


def test_show_computes_graph_before_connection_closes(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="show lifecycle", assignee="worker")

    args = argparse.Namespace(
        task_id=task_id,
        json=False,
        state_type=None,
        state_name=None,
    )

    assert cli._cmd_show(args) == 0
    assert f"Task {task_id}: show lifecycle" in capsys.readouterr().out
