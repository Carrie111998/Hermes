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


def test_kanban_create_stamps_session_id_from_flag(kanban_home):
    """`create --session` stamps session_id on the new task (#92583), the
    same field `create --session` did nothing with before this — only
    `list --session` could filter on it."""
    raw = kc.run_slash("create --session chat-99 --json my-task")
    payload = json.loads(raw)
    assert payload["session_id"] == "chat-99"


def test_kanban_create_subscribes_tui_session_via_session_key(kanban_home, monkeypatch):
    """CLI create running inside a TUI/desktop session (HERMES_SESSION_KEY
    set, no gateway platform/chat_id) auto-subscribes the same way the
    kanban_create tool already does (#92583), so the TUI notification
    poller can wake the originating pane."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-cli-abc")

    raw = kc.run_slash("create --json tui-created-task")
    payload = json.loads(raw)
    task_id = payload["id"]

    with kb.connect() as conn:
        subs = list(kb.list_notify_subs(conn, task_id))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-cli-abc"


def test_kanban_create_session_flag_wins_over_env(kanban_home, monkeypatch):
    """`--session` takes precedence over `$HERMES_SESSION_ID` (#92583):
    an explicit flag is a deliberate override and must not be shadowed by
    whatever the calling session happens to export."""
    monkeypatch.setenv("HERMES_SESSION_ID", "env-session")

    raw = kc.run_slash("create --session flag-session --json my-task")
    payload = json.loads(raw)
    assert payload["session_id"] == "flag-session"


def test_kanban_create_subscribes_gateway_session(kanban_home, monkeypatch):
    """CLI create running inside a gateway (telegram/discord/etc) session
    (`HERMES_SESSION_PLATFORM`/`HERMES_SESSION_CHAT_ID` set) subscribes
    against that real chat, not the TUI fallback path (#92583)."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    raw = kc.run_slash("create --json gateway-created-task")
    payload = json.loads(raw)
    task_id = payload["id"]

    with kb.connect() as conn:
        subs = list(kb.list_notify_subs(conn, task_id))
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-42"


def test_kanban_create_does_not_subscribe_plain_cli_session(kanban_home, monkeypatch):
    """A bare CLI invocation with no session env has no persistent delivery
    channel, so no subscription row is written."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    raw = kc.run_slash("create --json plain-cli-task")
    payload = json.loads(raw)
    task_id = payload["id"]

    with kb.connect() as conn:
        subs = list(kb.list_notify_subs(conn, task_id))
    assert subs == []


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


