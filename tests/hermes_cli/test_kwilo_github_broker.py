from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.kwilo_github_broker import (
    BrokerContext,
    broker_execution_instruction,
    run_broker_command,
    verify_broker,
)


@pytest.fixture
def context(tmp_path: Path) -> BrokerContext:
    broker = tmp_path / "github_app_broker.py"
    broker.write_text("# test broker\n", encoding="utf-8")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    return BrokerContext(
        persona="forge",
        repository="Hello-Kwilo/Kwilo-Site",
        broker_path=broker,
        workspace=workspace,
    )


def test_verify_uses_active_python_and_repository_scoped_identity(
    context, monkeypatch
):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "VERIFIED forge: app=hello-kwilo-forge "
                "repository=Hello-Kwilo/Kwilo-Site"
            ),
            stderr="",
        )

    monkeypatch.setenv("GH_TOKEN", "ambient-personal-token")
    monkeypatch.setattr(subprocess, "run", fake_run)

    verify_broker(context)

    argv, kwargs = calls[0]
    assert argv[1:] == [
        str(context.broker_path),
        "verify",
        "forge",
        "--repo",
        "Hello-Kwilo/Kwilo-Site",
    ]
    assert kwargs["cwd"] == str(context.workspace)
    assert "GH_TOKEN" not in kwargs["env"]
    assert "GITHUB_TOKEN" not in kwargs["env"]


def test_brokered_git_command_is_argv_only_and_fails_loud(context, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="repository permission denied",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_broker_command(context, "git", ["push", "-u", "origin", "HEAD"])

    assert calls[0][0][1:] == [
        str(context.broker_path),
        "git",
        "forge",
        "--repo",
        "Hello-Kwilo/Kwilo-Site",
        "--",
        "push",
        "-u",
        "origin",
        "HEAD",
    ]
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "permission denied" in result["stderr"]


def test_instruction_routes_network_operations_through_mcp_tools(context):
    instruction = broker_execution_instruction(context)

    assert "github_broker_git" in instruction
    assert "github_broker_gh" in instruction
    assert "gh auth status" in instruction
    assert "Hello-Kwilo/Kwilo-Site" in instruction
    assert '["add", "--", ...]' in instruction
    assert '["commit", "-m", "..."]' in instruction


def test_brokered_local_add_is_scoped_to_explicit_worktree_paths(
    context, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs))
            or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )

    result = run_broker_command(
        context,
        "git",
        ["add", "--", "package.json", "README.md"],
    )

    assert result["ok"] is True
    assert calls[0][0] == [
        "git",
        "add",
        "--",
        "package.json",
        "README.md",
    ]
    assert calls[0][1]["cwd"] == str(context.workspace)
    assert "GH_TOKEN" not in calls[0][1]["env"]


def test_brokered_local_add_executes_without_network_broker(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
    )
    tracked = workspace / "README.md"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", "README.md"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    tracked.write_text("after\n", encoding="utf-8")
    broker = tmp_path / "github_app_broker.py"
    broker.write_text("# unused for local commands\n", encoding="utf-8")
    local_context = BrokerContext(
        persona="forge",
        repository="Hello-Kwilo/Kwilo-Site",
        broker_path=broker,
        workspace=workspace,
    )

    result = run_broker_command(
        local_context,
        "git",
        ["add", "--", "README.md"],
    )

    assert result["ok"] is True
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert staged == ["README.md"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["-C", "..", "status"],
        ["add", "--", "../outside.txt"],
        ["add", "--all"],
        ["commit", "--amend"],
        ["reset", "--hard"],
    ],
)
def test_brokered_local_git_rejects_scope_escape(context, arguments):
    with pytest.raises(ValueError):
        run_broker_command(context, "git", arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["pr", "view", "43", "--repo", "Hello-Kwilo/Kwilo"],
        ["pr", "view", "43", "--repo=Hello-Kwilo/Kwilo"],
        ["pr", "view", "43", "-R", "Hello-Kwilo/Kwilo"],
        ["pr", "view", "43", "-RHello-Kwilo/Kwilo"],
    ],
)
def test_brokered_gh_rejects_repository_override(context, arguments):
    with pytest.raises(ValueError, match="may not override"):
        run_broker_command(context, "gh", arguments)
