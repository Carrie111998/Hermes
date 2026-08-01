"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

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


def _task_status(conn, task_id):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


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








# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False, **expectations):
    values = {
        "task_id": task_id,
        "reason": list(reason or []),
        "ids": list(ids or []) or None,
        "force": force,
        "dry_run": dry_run,
        "json": as_json,
        "expect_status": expectations.get("expect_status"),
    }
    values.update({
        key: expectations[key]
        for key in (
            "expect_current_run_id",
            "expect_latest_run_id",
            "expect_latest_event_id",
        )
        if key in expectations
    })
    return argparse.Namespace(**values)


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


def test_cli_promote_bulk_rejects_cas_before_any_mutation(kanban_home, capsys):
    with kb.connect() as conn:
        first, _ = _stuck_todo(conn)
        second, _ = _stuck_todo(conn)
        first_event = _latest_event_id(conn, first)

    rc = kb_cli._cmd_promote(
        _promote_ns(
            first,
            ids=[second],
            as_json=True,
            expect_latest_event_id=first_event,
        )
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert [item["task_id"] for item in payload] == [first, second]
    assert {
        item["code"] for item in payload
    } == {"cas_expectations_require_single_task"}
    with kb.connect() as conn:
        assert _task_status(conn, first) == "todo"
        assert _task_status(conn, second) == "todo"


def test_cli_promote_rejects_duplicate_bulk_syntax_with_cas(kanban_home, capsys):
    with kb.connect() as conn:
        task, _ = _stuck_todo(conn)
        event_id = _latest_event_id(conn, task)

    rc = kb_cli._cmd_promote(
        _promote_ns(
            task,
            ids=[task],
            as_json=True,
            expect_latest_event_id=event_id,
        )
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == task
    assert payload["code"] == "cas_expectations_require_single_task"
    with kb.connect() as conn:
        assert _task_status(conn, task) == "todo"


def _latest_event_id(conn, task_id):
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def _insert_run(conn, task_id, *, active=False):
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, ?, ?)",
        (task_id, "running" if active else "completed", 1),
    )
    run_id = int(cur.lastrowid)
    if active:
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id)
        )
    return run_id


def test_promote_cas_success_binds_all_observed_state(conn):
    child, _ = _stuck_todo(conn)
    event_id = _latest_event_id(conn, child)

    ok, err = kb.promote_task(
        conn,
        child,
        actor="operator",
        expected_status="todo",
        expected_current_run_id=None,
        expected_latest_run_id=None,
        expected_latest_event_id=event_id,
    )

    assert (ok, err) == (True, None)
    assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("status", "expected_status_mismatch"),
        ("current_run", "expected_current_run_id_mismatch"),
        ("latest_run", "expected_latest_run_id_mismatch"),
    ],
)
def test_promote_refuses_stale_task_or_run_observation(conn, mutation, expected_code):
    child, _ = _stuck_todo(conn)
    if mutation == "status":
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (child,))
    elif mutation == "current_run":
        _insert_run(conn, child, active=True)
    else:
        _insert_run(conn, child)

    ok, refusal = kb.promote_task(
        conn,
        child,
        actor="operator",
        force=True,
        expected_status="todo",
        expected_current_run_id=None,
        expected_latest_run_id=None,
    )

    assert ok is False
    assert refusal is not None
    assert refusal.code == expected_code
    assert kb.get_task(conn, child).status != "ready"


@pytest.mark.parametrize("mutation", ["comment", "link", "event"])
def test_promote_refuses_stale_latest_event_after_task_activity(conn, mutation):
    child, _ = _stuck_todo(conn)
    observed_event = _latest_event_id(conn, child)
    if mutation == "comment":
        kb.add_comment(conn, child, "reviewer", "new evidence")
    elif mutation == "link":
        new_parent = kb.create_task(conn, title="late dependency")
        kb.link_tasks(conn, new_parent, child)
    else:
        with kb.write_txn(conn):
            kb._append_event(conn, child, "external_observation")

    ok, refusal = kb.promote_task(
        conn,
        child,
        actor="operator",
        force=True,
        expected_latest_event_id=observed_event,
    )

    assert ok is False
    assert refusal is not None
    assert refusal.code == "expected_latest_event_id_mismatch"
    assert kb.get_task(conn, child).status != "ready"


def test_promote_revalidates_dependency_committed_by_competing_writer(conn):
    child, _ = _stuck_todo(conn)
    late_parent = kb.create_task(conn, title="late dependency")
    contender_started = Event()

    def contend():
        with kb.connect() as contender:
            contender_started.set()
            return kb.promote_task(contender, child, actor="operator")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (late_parent, child),
            )
            future = pool.submit(contend)
            assert contender_started.wait(timeout=2)
        ok, refusal = future.result(timeout=5)

    assert ok is False
    assert refusal is not None
    assert refusal.code == "unsatisfied_dependencies"
    task = kb.get_task(conn, child)
    assert task is not None and task.status == "todo"


def test_promote_nullable_expectations_distinguish_null_from_omitted(conn):
    null_child, _ = _stuck_todo(conn)
    ok, err = kb.promote_task(
        conn,
        null_child,
        actor="operator",
        expected_current_run_id=None,
        expected_latest_run_id=None,
    )
    assert (ok, err) == (True, None)

    changed_child, _ = _stuck_todo(conn)
    active_run = _insert_run(conn, changed_child, active=True)
    ok, refusal = kb.promote_task(
        conn,
        changed_child,
        actor="operator",
        expected_current_run_id=None,
    )
    assert ok is False
    assert refusal is not None
    assert refusal.code == "expected_current_run_id_mismatch"

    # Omitting both expectations preserves the legacy behavior.
    ok, err = kb.promote_task(conn, changed_child, actor="operator")
    assert (ok, err) == (True, None)
    assert active_run is not None


def test_promote_dry_run_has_zero_durable_effect(conn):
    child, _ = _stuck_todo(conn)
    before_events = _latest_event_id(conn, child)

    ok, err = kb.promote_task(
        conn,
        child,
        actor="operator",
        dry_run=True,
        expected_status="todo",
        expected_current_run_id=None,
        expected_latest_run_id=None,
        expected_latest_event_id=before_events,
    )

    assert (ok, err) == (True, None)
    assert kb.get_task(conn, child).status == "todo"
    assert _latest_event_id(conn, child) == before_events


def test_two_cas_contenders_only_one_promotes(kanban_home):
    with kb.connect() as setup:
        child, _ = _stuck_todo(setup)
        event_id = _latest_event_id(setup, child)

    def contend():
        with kb.connect() as contender:
            ok, refusal = kb.promote_task(
                contender,
                child,
                actor="operator",
                expected_status="todo",
                expected_current_run_id=None,
                expected_latest_run_id=None,
                expected_latest_event_id=event_id,
            )
            return ok, getattr(refusal, "code", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: contend(), range(2)))

    assert sorted(ok for ok, _ in results) == [False, True]
    assert {code for ok, code in results if not ok} <= {
        "expected_status_mismatch",
        "expected_latest_event_id_mismatch",
    }


def _run_cli(home, *args):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_real_cli_promote_cas_success_and_machine_readable_refusal(kanban_home):
    with kb.connect() as conn:
        success_child, _ = _stuck_todo(conn)
        success_event = _latest_event_id(conn, success_child)
        stale_child, _ = _stuck_todo(conn)
        stale_event = _latest_event_id(conn, stale_child)
        kb.add_comment(conn, stale_child, "reviewer", "changed")

    success = _run_cli(
        kanban_home,
        "promote",
        success_child,
        "--expect-status",
        "todo",
        "--expect-current-run-id",
        "none",
        "--expect-latest-run-id",
        "none",
        "--expect-latest-event-id",
        str(success_event),
        "--json",
    )
    assert success.returncode == 0, success.stderr
    success_payload = json.loads(success.stdout)
    assert success_payload["promoted"] is True
    assert success_payload["code"] is None

    refused = _run_cli(
        kanban_home,
        "promote",
        stale_child,
        "--force",
        "--expect-latest-event-id",
        str(stale_event),
        "--json",
    )
    assert refused.returncode == 1
    refusal_payload = json.loads(refused.stdout)
    assert refusal_payload["promoted"] is False
    assert refusal_payload["code"] == "expected_latest_event_id_mismatch"
    assert refusal_payload["refusal_reason"]
