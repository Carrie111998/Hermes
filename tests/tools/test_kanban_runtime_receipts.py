from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="runtime-receipt", assignee="test-worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _identity():
    return {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "responses",
        "session_id": "sess_actual",
        "source": "agent_runtime_after_provider_response",
    }


def test_complete_stamps_trusted_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_actual")
    out = json.loads(
        kt._handle_complete(
            {
                "summary": "verified",
                "metadata": {
                    "runtime_identity": {
                        "provider": "spoofed",
                        "model": "spoofed",
                    }
                },
            },
            runtime_identity=_identity(),
        )
    )
    assert out["ok"] is True
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run is not None
        assert run.metadata["worker_session_id"] == "sess_actual"
        assert run.metadata["runtime_identity"] == _identity()
    finally:
        conn.close()


def test_block_stamps_trusted_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_actual")
    out = json.loads(
        kt._handle_block(
            {"reason": "external input required", "kind": "needs_input"},
            runtime_identity=_identity(),
        )
    )
    assert out["ok"] is True
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run is not None
        assert run.metadata["runtime_identity"] == _identity()
    finally:
        conn.close()


def test_review_and_changes_runs_stamp_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_implementer")
    review = json.loads(
        kt._handle_request_review(
            {"summary": "ready for independent review", "reviewer": "reviewer"},
            runtime_identity=_identity(),
        )
    )
    assert review.get("ok") is True, review

    conn = kb.connect()
    try:
        implementation_run = kb.latest_run(conn, worker_env)
        assert implementation_run is not None
        assert implementation_run.metadata is not None
        assert implementation_run.metadata["runtime_identity"] == _identity()
        claimed = kb.claim_review_task(conn, worker_env)
        assert claimed is not None
        review_run_id = claimed.current_run_id
    finally:
        conn.close()

    reviewer_identity = {
        **_identity(),
        "model": "gpt-5.6-sol",
        "session_id": "sess_reviewer",
    }
    monkeypatch.setenv("HERMES_SESSION_ID", "sess_reviewer")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review_run_id))
    changes = json.loads(
        kt._handle_request_changes(
            {"reason": "one correction required"},
            runtime_identity=reviewer_identity,
        )
    )
    assert changes["ok"] is True

    conn = kb.connect()
    try:
        review_run = kb.latest_run(conn, worker_env)
        assert review_run is not None
        assert review_run.metadata is not None
        assert review_run.outcome == "changes_requested"
        assert review_run.metadata["runtime_identity"] == reviewer_identity
    finally:
        conn.close()


def test_successful_lifecycle_handoff_latches_worker_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    assert _record_successful_kanban_handoff(
        agent,
        "kanban_request_review",
        json.dumps({"ok": True, "task_id": "t_1", "run_id": 7, "status": "review"}),
    )
    assert agent._kanban_lifecycle_handoff == {
        "tool": "kanban_request_review",
        "task_id": "t_1",
        "run_id": 7,
        "status": "review",
    }


def test_failed_lifecycle_call_does_not_latch_worker_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_request_changes",
        json.dumps({"error": "ownership mismatch"}),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_delegate_child_does_not_latch_parent_worker_stop(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    with non_dispatcher_owned_context():
        assert not _record_successful_kanban_handoff(
            agent,
            "kanban_complete",
            json.dumps({"ok": True, "task_id": "t_parent", "run_id": 4}),
        )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_orchestrator_without_worker_env_does_not_latch_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_complete",
        json.dumps({"ok": True, "task_id": "t_explicit", "run_id": 9}),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")
