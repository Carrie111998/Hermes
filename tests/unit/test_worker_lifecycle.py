"""Focused tests for production worker lifecycle JSONL emission."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from hermes_cli.worker_lifecycle import (
    build_terminal_event,
    emit_start_identity_event,
    emit_terminal_event,
    process_birth_token,
)


def test_process_birth_token_is_identical_across_parent_and_child_queries():
    pid = os.getpid()
    parent_before = process_birth_token(pid)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.worker_lifecycle import process_birth_token; "
            "import sys; print(process_birth_token(int(sys.argv[1])) or '')",
            str(pid),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    parent_after = process_birth_token(pid)

    assert parent_before
    assert child.stdout.strip() == parent_before == parent_after


def test_process_birth_token_returns_none_for_nonexistent_pid():
    assert process_birth_token(2_147_483_647) is None


def test_loaded_worker_credential_scope_reaches_typed_transient_exit(
    monkeypatch, tmp_path
):
    """The trusted runtime load must bind non-secret scope to typed evidence."""
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli import runtime_provider
    from hermes_state import SessionDB

    event_path = tmp_path / "attempt.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_EVENT_PATH", str(event_path))
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_ATTEMPT", "1")
    monkeypatch.setenv("HERMES_WORKER_START_NONCE", "nonce-attempt-1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-credential-scope")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "worker-session-1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(worktree))
    monkeypatch.setenv("HERMES_PROFILE", "alpha")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "fixture-secret-never-persist",
            "base_url": "https://provider.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        },
    )

    state_db = SessionDB(tmp_path / "profile" / "state.db")
    cli = SimpleNamespace(
        requested_provider="openrouter",
        _explicit_api_key=None,
        _explicit_base_url=None,
        _fallback_model=[],
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        provider="openrouter",
        api_key=None,
        base_url=None,
        agent=None,
        model="fixture-model",
        _active_agent_route_signature=None,
        _normalize_model_for_provider=lambda _provider: False,
        _session_db=state_db,
    )

    try:
        assert CLIAgentSetupMixin._ensure_runtime_credentials(cli)
        generation = os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]
        assert generation.isdecimal() and int(generation) > 0
        assert os.environ["HERMES_PROVIDER"] == "openrouter"

        event = build_terminal_event(
            {"failed": True, "failure_reason": "transient_provider"},
            session_id="worker-session-1",
            exit_code=75,
        )

        assert event is not None
        assert (
            event["profile"],
            event["provider"],
            event["credential_generation"],
        ) == ("alpha", "openrouter", int(generation))
        assert "fixture-secret-never-persist" not in json.dumps(event)
    finally:
        state_db.close()


def test_loaded_worker_rotates_generation_when_effective_credential_changes(
    monkeypatch, tmp_path
):
    """Credential reloads rotate recovery scope without persisting secrets."""
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli import runtime_provider
    from hermes_state import SessionDB

    first_credential = "synthetic-first-credential-never-persist"
    second_credential = "synthetic-second-credential-never-persist"
    runtime = {
        "api_key": first_credential,
        "base_url": "https://provider.invalid/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
    }
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv(
        "HERMES_WORKER_LIFECYCLE_EVENT_PATH", str(tmp_path / "attempt.jsonl")
    )
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_ATTEMPT", "1")
    monkeypatch.setenv("HERMES_WORKER_START_NONCE", "nonce-credential-rotation")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-credential-rotation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "18")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "worker-session-rotation")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(worktree))
    monkeypatch.setenv("HERMES_PROFILE", "alpha")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: dict(runtime),
    )

    state_path = tmp_path / "profile" / "state.db"

    def make_loader(state_db):
        return SimpleNamespace(
            requested_provider="openrouter",
            _explicit_api_key=None,
            _explicit_base_url=None,
            _fallback_model=[],
            api_mode="chat_completions",
            acp_command=None,
            acp_args=[],
            provider="openrouter",
            api_key=None,
            base_url=None,
            agent=None,
            model="fixture-model",
            _active_agent_route_signature=None,
            _normalize_model_for_provider=lambda _provider: False,
            _session_db=state_db,
        )

    state_db = SessionDB(state_path)
    loader = make_loader(state_db)
    stale_state_db = None
    try:
        assert CLIAgentSetupMixin._ensure_runtime_credentials(loader)
        first_generation = int(
            os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]
        )
        assert first_generation > 0

        assert CLIAgentSetupMixin._ensure_runtime_credentials(loader)
        assert int(os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]) == (
            first_generation
        )

        stale_state_db = SessionDB(state_path)
        stale_loader = make_loader(stale_state_db)
        assert CLIAgentSetupMixin._ensure_runtime_credentials(stale_loader)
        assert int(os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]) == (
            first_generation
        )

        runtime["api_key"] = second_credential
        assert CLIAgentSetupMixin._ensure_runtime_credentials(loader)
        rotated_generation = int(
            os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]
        )
        assert rotated_generation > 0
        assert rotated_generation != first_generation

        assert CLIAgentSetupMixin._ensure_runtime_credentials(stale_loader)
        assert int(os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]) == (
            rotated_generation
        )
    finally:
        if stale_state_db is not None:
            stale_state_db.close()
        state_db.close()

    fresh_state_db = SessionDB(state_path)
    fresh_loader = make_loader(fresh_state_db)
    try:
        assert CLIAgentSetupMixin._ensure_runtime_credentials(fresh_loader)
        assert int(os.environ["HERMES_PROVIDER_CREDENTIAL_GENERATION"]) == (
            rotated_generation
        )
        event = build_terminal_event(
            {"failed": True, "failure_reason": "transient_provider"},
            session_id="worker-session-rotation",
            exit_code=75,
        )
        assert event is not None
        lifecycle_output = json.dumps(event)
    finally:
        fresh_state_db.close()

    with sqlite3.connect(state_path) as connection:
        durable_metadata = repr(
            connection.execute("SELECT key, value FROM state_meta").fetchall()
        )
    durable_bytes = b"".join(
        path.read_bytes() for path in state_path.parent.glob("state.db*")
    )
    for credential in (first_credential, second_credential):
        assert credential not in lifecycle_output
        assert credential not in durable_metadata
        assert credential.encode() not in durable_bytes


def test_emit_terminal_event_writes_typed_identity_bound_failure(monkeypatch, tmp_path):
    event_path = tmp_path / "events" / "attempt.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_EVENT_PATH", str(event_path))
    monkeypatch.setenv("HERMES_WORKER_LIFECYCLE_ATTEMPT", "1")
    monkeypatch.setenv("HERMES_WORKER_START_NONCE", "nonce-attempt-1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "worker-session-1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(worktree))
    monkeypatch.chdir(worktree)

    assert emit_start_identity_event(session_id="worker-session-1")
    assert emit_terminal_event(
        {"failed": True, "failure_reason": "transient_provider"},
        session_id="worker-session-1",
        exit_code=75,
    )

    records = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["kind"] for record in records] == ["identity", "terminal"]
    event = records[1]
    assert event == {
        "attempt": 1,
        "classification": "transient_provider",
        "exit_kind": "code",
        "exit_value": 75,
        "failure_reason": "transient_provider",
        "kind": "terminal",
        "root_pid": os.getpid(),
        "process_birth_token": process_birth_token(os.getpid()),
        "run_id": 17,
        "schema_version": 3,
        "nonce": "nonce-attempt-1",
        "expected_session_id": "worker-session-1",
        "observed_session_id": "worker-session-1",
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
    monkeypatch.setenv("HERMES_WORKER_START_NONCE", "nonce-attempt-2")
    monkeypatch.setenv("HERMES_WORKER_SESSION_ID", "session-17")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert emit_start_identity_event(session_id="session-17")
    assert emit_terminal_event(
        {"failed": True, "failure_reason": "rate_limit"},
        session_id="session-17",
        exit_code=75,
    )
    records = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["kind"] for record in records] == ["identity", "terminal"]
    payload = records[1]
    assert payload == {
        "schema_version": 3,
        "kind": "terminal",
        "nonce": "nonce-attempt-2",
        "task_id": "task-1",
        "run_id": 17,
        "attempt": 2,
        "expected_session_id": "session-17",
        "observed_session_id": "session-17",
        "worktree": str(tmp_path.resolve()),
        "root_pid": os.getpid(),
        "process_birth_token": process_birth_token(os.getpid()),
        "exit_kind": "code",
        "exit_value": 75,
        "failure_reason": "rate_limit",
        "classification": "rate_limited",
    }

    successor_payload = build_terminal_event(
        {"failed": True, "failure_reason": "transient_provider"},
        session_id="compression-successor",
        exit_code=75,
    )
    assert successor_payload is not None
    assert successor_payload["expected_session_id"] == "session-17"
    assert successor_payload["observed_session_id"] == "compression-successor"
