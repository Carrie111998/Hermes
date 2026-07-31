"""Focused tests for production worker lifecycle JSONL emission."""

import json
import os
from pathlib import Path

from hermes_cli.worker_lifecycle import emit_terminal_event


def test_emit_terminal_event_writes_typed_identity_bound_failure(monkeypatch, tmp_path):
    event_path = tmp_path / "events" / "attempt.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_EVENT_PATH", str(event_path))
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_ATTEMPT", "1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "worker-session-1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(worktree))

    assert emit_terminal_event(
        {"failed": True, "failure_reason": "transient_provider"},
        session_id="worker-session-1",
        exit_code=75,
    )

    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event == {
        "attempt": 1,
        "classification": "transient_provider",
        "exit_code": 75,
        "failure_reason": "transient_provider",
        "kind": "terminal",
        "owner_pid": os.getpid(),
        "run_id": 17,
        "schema_version": 1,
        "session_id": "worker-session-1",
        "task_id": "task-1",
        "worktree": str(worktree.resolve()),
    }


    event_path = tmp_path / "attempt.jsonl"
    monkeypatch.delenv("HERMES_WORKER_LIFECYCLE_EVENT_PATH", raising=False)
    emit_terminal_event(
        {"failed": True, "failure_reason": "billing"},
        session_id="session",
        exit_code=75,
    )
    assert not event_path.exists()



def test_terminal_event_schema_is_typed_and_identity_bound(tmp_path, monkeypatch):
    event_path = tmp_path / "event.json"
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_EVENT_PATH", str(event_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_ATTEMPT", "2")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "session-17")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))

    assert emit_terminal_event(
        {"failed": True, "failure_reason": "rate_limit"},
        session_id="session-17",
        exit_code=75,
    )
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "kind": "terminal",
        "task_id": "task-1",
        "run_id": 17,
        "attempt": 2,
        "session_id": "session-17",
        "worktree": str(tmp_path.resolve()),
        "owner_pid": os.getpid(),
        "exit_code": 75,
        "failure_reason": "rate_limit",
        "classification": "rate_limited",
    }

    assert not emit_terminal_event(
        {"failed": True, "failure_reason": "transient_provider"},
        session_id="wrong-session",
        exit_code=75,
    )
