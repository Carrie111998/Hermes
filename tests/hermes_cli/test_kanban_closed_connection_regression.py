"""Regression test for https://github.com/mjbfg1995/agent-control/issues/11

In hermes_cli/kanban.py _cmd_show() display mode, task_graph_context(conn,
task.id) was called after the connect_closing() with-block had already closed
the connection, raising:

    sqlite3.ProgrammingError: Cannot operate on a closed database.

The fix (applied in kanban.py) pre-computes the graph inside the with-block
while the connection is still open, then passes the cached graph to the
diagnostics code that runs after the block exits.

See: https://github.com/mjbfg1995/agent-control/issues/11
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. Direct unit-level reproduction
# ---------------------------------------------------------------------------

def test_task_graph_context_raises_on_closed_connection(kanban_home):
    """Directly reproduce the raw sqlite3 error that the old code hit.

    Before the fix, _cmd_show display mode exited the connect_closing()
    context and then called task_graph_context on the now-closed connection.
    This test documents that the raw call on a closed connection does
    raise the expected sqlite3.ProgrammingError.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="closed-conn regression task",
            assignee="programmer",
        )

    # Old buggy pattern: conn is closed, then task_graph_context is called.
    conn = kb.connect()
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        kb.task_graph_context(conn, task_id)


def test_task_graph_context_precompute_avoids_closed_db_error(kanban_home):
    """Fixed pattern: pre-compute graph inside connect_closing(), reuse after.

    Mirrors the real _cmd_show flow:

    1. Open connection via connect_closing()
    2. Fetch task and related data while conn is open
    3. Pre-compute graph = task_graph_context(conn, task.id) inside the block
    4. Exit the with-block (conn closes)
    5. Use the cached graph in diagnostics code that runs after

    The test asserts that no sqlite3.ProgrammingError is raised and the
    returned graph has the expected shape.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="precompute regression task",
            assignee="programmer",
        )

    # Fixed pattern from _cmd_show (display mode):
    #   fetch graph inside connect_closing(), use it after.
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        # This is the fix: pre-compute while conn is still open.
        graph = kb.task_graph_context(conn, task_id)

    # After the with-block conn is closed, but graph is already cached.
    # The old code would have crashed here by calling task_graph_context
    # on the closed conn.  With the fix, graph is a plain dict and safe.
    assert isinstance(graph, dict)
    assert "parents" in graph
    assert "children" in graph
    assert graph == {"parents": [], "children": []}


# ---------------------------------------------------------------------------
# 2. End-to-end: _cmd_show display mode
# ---------------------------------------------------------------------------

def test_cmd_show_display_mode_no_closed_db_error(kanban_home):
    """Regression for #11: _cmd_show display mode must not crash.

    This is the actual end-to-end path that the bug report describes:
    a programmatic (non-JSON) caller of ``hermes kanban show`` hits the
    diagnostics section after connect_closing() has closed the DB.
    """
    from hermes_cli.kanban import _cmd_show

    # Create a real task in the isolated DB
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="regression task",
            assignee="tester",
            body="test body for diagnostics",
        )

    # Mimic display-mode args (no --json flag → falls through to display)
    args = SimpleNamespace(
        task_id=task_id,
        json=False,
        state_type=None,
        state_name=None,
    )

    # Before the fix this raised:
    #   sqlite3.ProgrammingError: Cannot operate on a closed database.
    # After the fix it returns 0 (success).
    assert _cmd_show(args) == 0
