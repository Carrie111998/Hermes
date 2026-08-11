"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import io
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


def test_human_show_reads_graph_before_connection_closes_and_prints_body(
    kanban_home,
):
    """Pryor's CLI path must not mistake a complete body for a blank card."""
    body = "First line of the stored brief.\nSecond line proves multiline output."
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="show body regression", body=body, assignee="stark"
        )

    output = kc.run_slash(f"show {task_id}")

    assert "Cannot operate on a closed database" not in output
    assert "Body:\n" + body in output


def test_create_dash_reads_multiline_stdin_through_real_cli_path(
    kanban_home, monkeypatch
):
    body = "Known first line from stdin.\nKnown second line from stdin."
    monkeypatch.setattr("sys.stdin", io.StringIO(body))

    output = kc.run_slash(
        "create 'stdin body regression' --body - --initial-status blocked"
    )

    assert output.startswith("Created t_")
    with kb.connect_closing() as conn:
        task = kb.list_tasks(conn, limit=1)[0]
    assert task.body == body


@pytest.mark.parametrize("body", ["", "   ", "-", "--", "n/a", "tbd", "."])
def test_create_rejects_blank_or_placeholder_body_before_db_write(
    kanban_home, monkeypatch, body
):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    quoted = json.dumps(body)

    output = kc.run_slash(f"create 'invalid body regression' --body={quoted}")

    assert "refusing to create" in output or "stdin was empty" in output
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn, limit=10) == []


def test_long_body_survives_python_argv_db_dispatch_worker_and_monitor(
    kanban_home,
    all_assignees_spawnable,
):
    """One real card keeps one exact brief through every live read boundary."""
    body = "\n".join(
        f"Acceptance line {index:03d}: preserve this exact multiline instruction."
        for index in range(120)
    )

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args([
        "kanban",
        "create",
        "full task body end-to-end",
        "--body",
        body,
        "--assignee",
        "stark",
    ])

    assert kc.kanban_command(args) == 0
    with kb.connect_closing() as conn:
        task = next(
            row
            for row in kb.list_tasks(conn, limit=100)
            if row.title == "full task body end-to-end"
        )
        task_id = task.id
        assert task.body == body

    captured = {}

    def spawn(claimed, workspace, board=None):
        captured["claim_body"] = claimed.body
        captured["workspace"] = workspace
        return None

    with kb.connect_closing() as conn:
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            reconcile_orphans=False,
        )
        claimed = kb.get_task(conn, task_id)
        worker_context = kb.build_worker_context(conn, task_id)

    assert task_id in [row[0] for row in result.spawned]
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.body == body
    assert captured["claim_body"] == body
    assert Path(captured["workspace"]).is_dir()
    assert body in worker_context

    human_show = kc.run_slash(f"show {task_id}")
    assert "Body:\n" + body in human_show

    from plugins.kanban.dashboard import plugin_api

    monitor_detail = plugin_api.get_task(
        task_id,
        board=None,
        run_state_type=None,
        run_state_name=None,
    )
    assert monitor_detail["task"]["body"] == body

    with kb.connect_closing() as conn:
        assert kb.delete_task(conn, task_id) is True
        assert kb.get_task(conn, task_id) is None


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
        args = parser.parse_args([
            "kanban", "--board", board, "create", title,
            "--body", f"Concurrency probe for board {board}.",
        ])
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

    out1 = kc.run_slash(
        "create 'stuck worker task' --assignee broken-model "
        "--body 'Simulate a worker that claims and then stalls.'"
    )
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


