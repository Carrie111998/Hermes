"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
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


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_list_json_handles_bytes_fields_in_db(kanban_home):
    """Regression: tasks with BLOB/bytes fields (e.g. body stored as bytes)
    must not crash `hermes kanban list --status archived --json` with TypeError."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, workspace_kind, created_at, created_by) "
            "VALUES (?, ?, ?, ?, 'archived', 0, 'scratch', 1000, 'user')",
            ("t_bytes1", "bytes task", b"## archived task body as bytes", "coder"),
        )
        conn.commit()

    raw = kc.run_slash("list --status archived --json")
    payload = json.loads(raw)
    assert any(
        row.get("id") == "t_bytes1"
        and row.get("body") == "## archived task body as bytes"
        for row in payload
    )


def test_kanban_list_json_handles_numeric_blob_fields_in_db(kanban_home):
    """Regression: tasks with BLOBs in numeric/timestamp columns (e.g. priority=b'7',
    created_at=b'1000', started_at=b'1010', completed_at=b'1020', max_retries=b'3')
    must not crash `hermes kanban list --status archived --json` with TypeError."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks ("
            "  id, title, body, assignee, status, priority, workspace_kind, "
            "  created_at, started_at, completed_at, max_retries, created_by"
            ") VALUES (?, ?, ?, ?, 'archived', ?, 'scratch', ?, ?, ?, ?, 'user')",
            ("t_numblob1", "num blob task", "body", "coder", b"7", b"1000", b"1010", b"1020", b"3"),
        )
        conn.commit()

    raw = kc.run_slash("list --status archived --json")
    payload = json.loads(raw)
    matched = [row for row in payload if row.get("id") == "t_numblob1"]
    assert len(matched) == 1
    t = matched[0]
    assert t["priority"] == 7
    assert t["created_at"] == 1000
    assert t["started_at"] == 1010
    assert t["completed_at"] == 1020
    assert t["max_retries"] == 3


def test_kanban_list_json_handles_status_blob_in_db(kanban_home):
    """Regression: tasks with BLOB status (e.g. status=b'archived') must be matched
    by exact `hermes kanban list --status archived --json` and `list --archived --json`."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks ("
            "  id, title, body, assignee, status, priority, workspace_kind, "
            "  created_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, 0, 'scratch', 1000, 'user')",
            ("t_statusblob", "status blob task", "body", "coder", b"archived"),
        )
        conn.execute(
            "INSERT INTO tasks ("
            "  id, title, body, assignee, status, priority, workspace_kind, "
            "  created_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, 0, 'scratch', 1001, 'user')",
            ("t_statusblob-control", "active control", "body", "coder", "ready"),
        )
        conn.commit()

    # Exact filter command: --status archived
    raw = kc.run_slash("list --status archived --json")
    payload = json.loads(raw)
    matched = [row for row in payload if row.get("id") == "t_statusblob"]
    assert len(matched) == 1
    assert matched[0]["id"] == "t_statusblob"
    assert matched[0]["title"] == "status blob task"
    assert matched[0]["assignee"] == "coder"
    assert matched[0]["status"] == "archived"

    # Also verify default list (which excludes archived) excludes the BLOB archived task
    raw_default = kc.run_slash("list --json")
    payload_default = json.loads(raw_default)
    assert payload_default
    assert any(row.get("id") == "t_statusblob-control" for row in payload_default)
    assert not any(row.get("id") == "t_statusblob" for row in payload_default)

    # And --archived flag includes it
    raw_archived = kc.run_slash("list --archived --json")
    payload_archived = json.loads(raw_archived)
    assert any(row.get("id") == "t_statusblob" for row in payload_archived)


def test_kanban_stats_handles_bytes_assignee_and_status(kanban_home):
    """Regression: tasks with BLOB assignee/status must not crash `hermes kanban stats`
    or `hermes kanban stats --json` when sorting."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO tasks ("
            "  id, title, body, assignee, status, priority, workspace_kind, "
            "  created_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, 0, 'scratch', 1000, 'user')",
            ("t_blob_stat1", "stat task 1", "body", b"coder", b"ready"),
        )
        conn.execute(
            "INSERT INTO tasks ("
            "  id, title, body, assignee, status, priority, workspace_kind, "
            "  created_at, created_by"
            ") VALUES (?, ?, ?, ?, ?, 0, 'scratch', 1000, 'user')",
            ("t_blob_stat2", "stat task 2", "body", "reviewer", "ready"),
        )
        conn.commit()

    text_output = kc.run_slash("stats")
    assert "coder" in text_output
    assert "reviewer" in text_output
    assert "ready=1" in text_output

    json_raw = kc.run_slash("stats --json")
    data = json.loads(json_raw)
    assert "coder" in data["by_assignee"]
    assert "reviewer" in data["by_assignee"]
    assert data["by_assignee"]["coder"]["ready"] == 1
    assert data["by_assignee"]["reviewer"]["ready"] == 1




def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


