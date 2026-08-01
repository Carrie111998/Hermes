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


# ---------------------------------------------------------------------------
# Verified completion flags (#70806)
# ---------------------------------------------------------------------------

def _created_task_id(out: str) -> str:
    import re
    m = re.search(r"(t_[a-f0-9]+)", out)
    assert m, f"no task id in output: {out!r}"
    return m.group(1)


def test_create_verify_cmd_flag_persists(kanban_home):
    out = kc.run_slash("create t --verify-cmd 'pytest -q'")
    assert "Created" in out
    tid = _created_task_id(out)
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.verify_mode == "cmd"
    assert t.verify_cmd == "pytest -q"
    show = kc.run_slash(f"show {tid}")
    assert "verify:    cmd: pytest -q" in show


def test_create_verify_auto_flag_persists(kanban_home):
    out = kc.run_slash("create t --verify auto")
    assert "Created" in out
    tid = _created_task_id(out)
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.verify_mode == "auto"
    assert t.verify_cmd is None
    show = kc.run_slash(f"show {tid}")
    assert "verify:    auto (ledger evidence)" in show


def test_create_verify_flags_mutually_exclusive(kanban_home):
    out = kc.run_slash("create t --verify-cmd 'pytest -q' --verify auto")
    assert "mutually exclusive" in out
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_create_json_includes_verify_fields(kanban_home):
    out = kc.run_slash("create gated --verify-cmd 'pytest -q' --json")
    d = json.loads(out)
    assert d["verify_mode"] == "cmd"
    assert d["verify_cmd"] == "pytest -q"

    plain = json.loads(kc.run_slash("create plain --json"))
    assert plain["verify_mode"] is None
    assert plain["verify_cmd"] is None


def test_show_omits_verify_line_without_config(kanban_home):
    out = kc.run_slash("create plain")
    tid = _created_task_id(out)
    show = kc.run_slash(f"show {tid}")
    assert "verify:" not in show


def test_cli_complete_refuses_gated_task_without_skip_verify(kanban_home):
    """The CLI is reachable from any worker's terminal tool, so an
    unflagged complete on a gated card must refuse, naming the override."""
    out = kc.run_slash("create gated --verify-cmd 'exit 1'")
    tid = _created_task_id(out)
    res = kc.run_slash(f"complete {tid}")
    assert "Completed" not in res
    assert "--skip-verify" in res
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_cli_skip_verify_refused_in_worker_context(kanban_home, monkeypatch):
    """A worker terminal process must not be able to waive its own gate:
    any HERMES_KANBAN_TASK in the environment refuses --skip-verify
    outright, flag or no flag."""
    out = kc.run_slash("create gated --verify-cmd 'exit 1'")
    tid = _created_task_id(out)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kc, "_waiver_tty_ok", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: tid)
    res = kc.run_slash(f"complete {tid} --skip-verify")
    assert "Completed" not in res
    assert "worker" in res.lower()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_cli_skip_verify_refused_without_tty(kanban_home):
    """Non-interactive invocations (scripts, worker terminal tools, cron)
    cannot waive: the waiver requires a human at a real terminal."""
    out = kc.run_slash("create gated --verify-cmd 'exit 1'")
    tid = _created_task_id(out)
    res = kc.run_slash(f"complete {tid} --skip-verify")
    assert "Completed" not in res
    assert "interactive" in res.lower()
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_cli_skip_verify_refused_on_wrong_confirmation(kanban_home, monkeypatch):
    out = kc.run_slash("create gated --verify-cmd 'exit 1'")
    tid = _created_task_id(out)
    monkeypatch.setattr(kc, "_waiver_tty_ok", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: "t_wrong")
    res = kc.run_slash(f"complete {tid} --skip-verify")
    assert "Completed" not in res
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status != "done"


def test_cli_skip_verify_confirmed_waives_and_records_audit(kanban_home, monkeypatch):
    """The human override: interactive terminal + typed task id. Leaves a
    durable trail (event + comment), not just an ephemeral stderr line."""
    out = kc.run_slash("create gated --verify-cmd 'exit 1'")
    tid = _created_task_id(out)
    monkeypatch.setattr(kc, "_waiver_tty_ok", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: tid)
    done = kc.run_slash(f"complete {tid} --skip-verify")
    assert "Completed" in done
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "verify_bypassed"]
        assert len(events) == 1
        assert events[0].payload["mode"] == "cmd"
        assert events[0].payload["flag"] == "--skip-verify"
        comments = [c for c in kb.list_comments(conn, tid)
                    if c.author == "verify-gate"]
        assert comments and "bypass" in comments[0].body


def test_verify_cmd_redacted_in_json_and_show_projections(kanban_home):
    """A secret-bearing verify command must not be echoed verbatim by any
    projection (JSON dict or human show) — the tests above prove inline
    credentials are a real usage, so projections redact like events do."""
    secret = "ghp_" + "Abc123XyZ0" * 3
    out = kc.run_slash(f"create gated --verify-cmd 'GH_TOKEN={secret} ./check.sh' --json")
    d = json.loads(out)
    assert secret not in d["verify_cmd"]
    assert "GH_TOKEN" in d["verify_cmd"]  # the shape survives, the secret doesn't
    tid = d["id"]
    show = kc.run_slash(f"show {tid}")
    assert secret not in show
    assert "verify:" in show


def test_create_verify_cmd_refused_on_unsupported_platform(kanban_home, monkeypatch):
    """Creation-time guard: opting a task into cmd-mode verification on a
    host that can never run the gate would strand it — refuse up front.
    (--verify auto stays available: the ledger read is pure Python.)"""
    from hermes_cli import kanban_verify as kv
    monkeypatch.setattr(kv, "platform_supported", lambda: False)
    out = kc.run_slash("create gated --verify-cmd 'pytest -q'")
    assert "Created" not in out
    assert "platform" in out.lower()
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn) == []
    auto = kc.run_slash("create ok --verify auto")
    assert "Created" in auto
