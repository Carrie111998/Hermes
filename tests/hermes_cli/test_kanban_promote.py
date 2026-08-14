"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_gate_inserted_after_preflight_cannot_be_overwritten(
    kanban_home, monkeypatch,
):
    """The not-before check and manual promotion must share one write txn."""
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000.0)

    with kb.connect() as setup_conn:
        task_id = kb.create_task(setup_conn, title="racy promotion")
        setup_conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,)
        )
        db_path = kb.kanban_db_path()

    preflight_done = threading.Event()
    allow_promotion = threading.Event()
    original_write_txn = kb.write_txn

    @contextlib.contextmanager
    def pause_before_promotion_txn(conn):
        if threading.current_thread() is not threading.main_thread():
            preflight_done.set()
            assert allow_promotion.wait(5)
        with original_write_txn(conn):
            yield
    monkeypatch.setattr(kb, "write_txn", pause_before_promotion_txn)


    outcome = []

    def promote_from_worker():
        with kb.connect(db_path=db_path) as worker_conn:
            outcome.append(
                kb.promote_task(
                    worker_conn, task_id, actor="worker", force=True
                )
            )

    worker = threading.Thread(target=promote_from_worker)
    worker.start()
    assert preflight_done.wait(5)

    with kb.connect(db_path=db_path) as racing_conn:
        with kb.write_txn(racing_conn):
            racing_conn.execute(
                "UPDATE tasks SET not_before = ? WHERE id = ?",
                ("2030-01-01T00:00:00Z", task_id),
            )

    allow_promotion.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome == [
        (False, "not-before deadline has not elapsed: 2030-01-01T00:00:00Z")
    ]

    with kb.connect(db_path=db_path) as verify_conn:
        task = kb.get_task(verify_conn, task_id)
        assert task.status == "todo"







# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kb.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


