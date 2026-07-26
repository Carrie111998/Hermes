"""Implementer-owned Task 3 bridge compatibility and migration checks."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from hermes_cli import kanban_db as kb
from tools import async_delegation as ad


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    board = tmp_path / "kanban.db"
    kb.init_db(board)
    ad._reset_for_tests()
    yield board
    ad._reset_for_tests()


def _wait(key: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = ad.get_logical_delegation(key)
        if row and row["state"] == "terminal_unattached":
            return row
        time.sleep(0.01)
    pytest.fail("logical delegation did not finish")


def _claimed_authority(board: Path, task_id: str):
    with kb.connect_closing(board) as conn:
        assert kb.claim_task(conn, task_id, claimer="bridge-claim") is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET workflow_template_id='workflow-v1', current_step_key='implement' WHERE id=?",
            (task_id,),
        )
        conn.commit()
    return ad.BridgeAuthorityContext(
        kanban_db_path=str(board.resolve()),
        workflow_id="workflow-v1",
        step_key="implement",
        step_attempt_id=f"workflow-v1/implement/{task.current_run_id}",
        task_id=task_id,
        run_id=task.current_run_id,
        claim_token="bridge-claim",
        lane="forge",
        route="openai-codex/gpt-5.6-sol",
        owner_id="parent-session",
    )


def test_terminal_digest_is_canonical_and_source_schema_migrates_idempotently(stores):
    result = {"status": "PASS", "summary": "ok", "nested": {"b": 2, "a": 1}}
    ad.dispatch_logical_delegation(
        logical_key="canonical/result", input_digest="sha256:request", goal="g",
        context=None, toolsets=None, role="leaf", model="m", session_key="s",
        parent_session_id="p", runner=lambda: result,
    )
    first = _wait("canonical/result")
    assert first["result_digest"] == ad.canonical_json_digest(
        {"nested": {"a": 1, "b": 2}, "summary": "ok", "status": "PASS"}
    )
    with ad._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(logical_delegations)")}
    with ad._connect():
        pass
    assert {"logical_key", "input_digest", "result_digest", "source_ack_count"} <= columns


def test_kanban_receipt_schema_is_additive_and_idempotent(stores):
    kb.init_db(stores)
    kb.init_db(stores)
    with sqlite3.connect(stores) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"delegation_receipts", "delegation_continuations"} <= tables


def test_receipt_target_conflict_preserves_authoritative_source(stores):
    with kb.connect_closing(stores) as conn:
        first_task = kb.create_task(
            conn,
            title="one",
            assignee="forge",
            created_by="loom",
            initial_status="running",
        )
        second_task = kb.create_task(conn, title="two", created_by="loom", initial_status="running")
    authority = _claimed_authority(stores, first_task)
    logical_key = ad.canonical_logical_key(authority)
    goal = "g"
    input_digest = ad.canonical_json_digest(
        ad.canonical_dispatch_input(goal, None, "leaf")
    )
    ad.dispatch_logical_delegation(
        logical_key=logical_key, input_digest=input_digest, goal=goal,
        context=None, toolsets=None, role="leaf", model="m", session_key="s",
        parent_session_id="p", runner=lambda: {"status": "PASS", "summary": "ok"},
        authority_context=authority,
    )
    _wait(logical_key)
    attached = ad.attach_logical_dispatch_receipt(
        logical_key=logical_key, kanban_db_path=stores, task_id=first_task,
        acknowledge_source=False,
    )
    conflict = ad.attach_logical_dispatch_receipt(
        logical_key=logical_key, kanban_db_path=stores, task_id=second_task,
        acknowledge_source=False,
    )
    assert attached["status"] == "committed"
    assert conflict["status"] == "conflict"
    source = ad.get_logical_delegation(logical_key)
    assert source["state"] == "receipted_unacknowledged"
    assert source["receipt_id"] == attached["receipt_id"]
    with sqlite3.connect(stores) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (first_task,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (second_task,),
        ).fetchone()[0] == 0


def test_enabled_delegate_bridge_replay_returns_before_transcript_or_child(stores, monkeypatch):
    import tools.delegate_tool as dt
    import tools.delegation_live_log as live_log

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "parent"
    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_load_config", lambda: {"workflow_bridge_enabled": True})
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *_a, **_k: creds)
    called = {"transcript": 0, "child": 0}
    monkeypatch.setattr(
        live_log, "create_live_transcripts",
        lambda *_a, **_k: called.__setitem__("transcript", called["transcript"] + 1),
    )
    monkeypatch.setattr(
        dt, "_build_child_agent",
        lambda **_k: called.__setitem__("child", called["child"] + 1),
    )
    reserved = ad.reserve_logical_delegation(
        logical_key="kanban/step/attempt", input_digest="sha256:stable", goal="g",
        context=None, toolsets=None, role="leaf", model="m",
    )
    assert reserved["status"] == "reserved"

    replay = json.loads(dt.delegate_task(
        goal="g", background=True, parent_agent=parent,
        logical_dispatch_key="kanban/step/attempt",
        logical_input_digest="sha256:stable",
    ))
    assert replay["status"] == "replayed"
    assert replay["delegation_id"] == reserved["delegation_id"]
    assert called == {"transcript": 0, "child": 0}
