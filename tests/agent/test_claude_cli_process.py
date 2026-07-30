from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.claude_cli_process import (
    ClaudeCLIAuthenticationError,
    ClaudeCLIExecutionError,
    ClaudeCLIProcessRunner,
    ClaudeCLIQuotaError,
    ClaudeCLIStaleSessionError,
    ClaudeCLITimeoutError,
    ClaudeCLIUnavailableError,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_claude_cli.py"


def fixture_runner(tmp_path, monkeypatch, *, mode="success", timeout=5):
    log_path = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    return (
        ClaudeCLIProcessRunner(
            executable=sys.executable,
            executable_args=[str(FIXTURE)],
            timeout_seconds=timeout,
        ),
        log_path,
    )


def complete(runner):
    return runner.complete(
        prompt='literal "$HOME" and ; are plain stdin',
        schema_json='{"type":"object"}',
        model="opus",
        new_session_id="22222222-2222-4222-8222-222222222222",
    )


def test_builds_discrete_argv_and_disables_claude_tools(tmp_path, monkeypatch):
    runner, _ = fixture_runner(tmp_path, monkeypatch)

    result = complete(runner)

    assert result.decision == {"kind": "final", "text": "ok"}
    assert result.session_id == "22222222-2222-4222-8222-222222222222"
    assert result.model_reported == "claude-opus-5"
    assert "--tools" in result.argv
    assert result.argv[result.argv.index("--tools") + 1] == ""
    assert result.shell is False


def test_child_environment_removes_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-child")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "never-child")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "never-child")
    monkeypatch.setenv("OPENAI_API_KEY", "never-child")
    runner, log_path = fixture_runner(tmp_path, monkeypatch, mode="dump-env")

    result = complete(runner)
    logged = json.loads(log_path.read_text(encoding="utf-8"))[0]

    assert result.decision["text"] == "secrets-absent"
    assert logged["environment"] == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_TOKEN": None,
        "CLAUDE_CODE_OAUTH_TOKEN": None,
        "OPENAI_API_KEY": None,
        "PATH": os.environ["PATH"],
    }


def test_auth_and_version_probes_use_cli_contract(tmp_path, monkeypatch):
    runner, _ = fixture_runner(tmp_path, monkeypatch)

    assert runner.version() == "2.1.220 (Claude Code)"
    assert runner.auth_status() == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }


def test_auth_probe_rejects_logged_out_or_non_first_party(tmp_path, monkeypatch):
    runner, _ = fixture_runner(tmp_path, monkeypatch, mode="logged-out")

    with pytest.raises(ClaudeCLIAuthenticationError):
        runner.auth_status()


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("auth-error", ClaudeCLIAuthenticationError),
        ("quota", ClaudeCLIQuotaError),
        ("stale-session", ClaudeCLIStaleSessionError),
        ("execution-error", ClaudeCLIExecutionError),
        ("malformed", ClaudeCLIExecutionError),
    ],
)
def test_classifies_process_failures(tmp_path, monkeypatch, mode, error_type):
    runner, _ = fixture_runner(tmp_path, monkeypatch, mode=mode)

    with pytest.raises(error_type):
        complete(runner)


def test_timeout_is_bounded_and_clears_active_process(tmp_path, monkeypatch):
    runner, _ = fixture_runner(tmp_path, monkeypatch, mode="timeout", timeout=0.1)

    with pytest.raises(ClaudeCLITimeoutError):
        complete(runner)

    assert runner.active_pid is None


def test_missing_executable_is_unavailable():
    runner = ClaudeCLIProcessRunner(executable="definitely-not-a-real-claude-command")

    with pytest.raises(ClaudeCLIUnavailableError):
        runner.version()
