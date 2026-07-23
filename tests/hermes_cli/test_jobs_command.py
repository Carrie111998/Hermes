from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.job_diagnostics import (
    JobRun,
    JobStateStore,
    TimingCategory,
    jobs_command,
    run_jobs_slash,
)
from hermes_cli.subcommands.jobs import build_jobs_parser


def _identity(path) -> dict:
    resolved = str(Path(path).resolve()) if path else None
    return {
        "available": True,
        "worktree": resolved,
        "repo_root": resolved,
        "branch": "test",
        "head": "a" * 40,
        "dirty": False,
        "status_digest": "b" * 64,
    }


def _populated_store(tmp_path: Path) -> JobStateStore:
    store = JobStateStore(
        tmp_path / "state",
        repository_probe=_identity,
    )
    JobRun.start(
        store,
        job_id="job-123",
        lane_id="tests",
        title="Test job",
        worktree=tmp_path,
        provider="openai-codex",
        model="gpt-test",
        read_only=True,
    ).define_phases([
        {
            "phase_id": "focused-tests",
            "category": TimingCategory.TEST,
            "command": "scripts/run_tests.sh tests/example.py",
        }
    ])
    return store


def test_jobs_parser_wires_every_read_only_action():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    handler = lambda args: args  # noqa: E731
    build_jobs_parser(subparsers, cmd_jobs=handler)

    assert parser.parse_args(["jobs"]).func is handler
    assert parser.parse_args(["jobs", "status"]).jobs_action == "status"
    assert parser.parse_args(["jobs", "why-slow", "j"]).jobs_action == "why-slow"
    assert parser.parse_args(["jobs", "parallel"]).jobs_action == "parallel"
    assert parser.parse_args(["jobs", "resume-plan", "j"]).jobs_action == "resume-plan"
    assert parser.parse_args(["jobs", "dashboard"]).jobs_action == "dashboard"
    assert parser.parse_args(["jobs", "why", "j"]).jobs_action == "why"
    assert parser.parse_args(["jobs", "resume", "j"]).jobs_action == "resume"


def test_cli_aliases_dispatch_to_canonical_reports(tmp_path, capsys):
    store = _populated_store(tmp_path)

    assert (
        jobs_command(
            SimpleNamespace(jobs_action="dashboard", json=False),
            store=store,
        )
        == 0
    )
    assert "Hermes job diagnostics" in capsys.readouterr().out

    assert (
        jobs_command(
            SimpleNamespace(
                jobs_action="why",
                job_id="job-123",
                lane=None,
                json=False,
            ),
            store=store,
        )
        == 0
    )
    assert "Why is job-123 slow?" in capsys.readouterr().out

    assert (
        jobs_command(
            SimpleNamespace(
                jobs_action="resume",
                job_id="job-123",
                lane="tests",
                json=False,
            ),
            store=store,
        )
        == 0
    )
    assert "Resume plan: SAFE" in capsys.readouterr().out


def test_empty_status_is_read_only_and_does_not_create_state_root(tmp_path, capsys):
    store = JobStateStore(tmp_path / "missing")
    args = SimpleNamespace(jobs_action="status", json=False)

    assert jobs_command(args, store=store) == 0
    output = capsys.readouterr().out
    assert "Active: 0" in output
    assert not store.root.exists()


def test_status_why_parallel_and_resume_commands(tmp_path, capsys):
    store = _populated_store(tmp_path)

    assert (
        jobs_command(
            SimpleNamespace(jobs_action="status", json=False),
            store=store,
        )
        == 0
    )
    assert "Provider utilization" in capsys.readouterr().out

    assert (
        jobs_command(
            SimpleNamespace(
                jobs_action="why-slow",
                job_id="job-123",
                lane=None,
                json=False,
            ),
            store=store,
        )
        == 0
    )
    why = capsys.readouterr().out
    assert "Why is job-123 slow?" in why
    assert "model_wait" in why

    assert (
        jobs_command(
            SimpleNamespace(jobs_action="parallel", job_id=None, json=False),
            store=store,
        )
        == 0
    )
    parallel = capsys.readouterr().out
    assert "nothing was launched" in parallel
    assert "job-123/tests" in parallel

    assert (
        jobs_command(
            SimpleNamespace(
                jobs_action="resume-plan",
                job_id="job-123",
                lane="tests",
                json=False,
            ),
            store=store,
        )
        == 0
    )
    resume = capsys.readouterr().out
    assert "Resume plan: SAFE" in resume
    assert "No command was launched" in resume


def test_slash_reports_match_cli_surface(tmp_path):
    store = _populated_store(tmp_path)

    assert "Hermes job diagnostics" in run_jobs_slash("/jobs", store=store)
    assert "Why is job-123 slow?" in run_jobs_slash(
        "/jobs why-slow job-123",
        store=store,
    )
    assert "nothing was launched" in run_jobs_slash(
        "/jobs parallel",
        store=store,
    )
    assert "Resume plan: SAFE" in run_jobs_slash(
        "/jobs resume-plan job-123 tests",
        store=store,
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


@pytest.mark.asyncio
async def test_gateway_jobs_handler_uses_read_only_shared_report(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.job_diagnostics.run_jobs_slash",
        lambda text: f"report for {text}",
    )
    runner = object.__new__(GatewayRunner)

    result = await runner._handle_jobs_command(_event("/jobs why-slow job-1"))

    assert result == "report for /jobs why-slow job-1"
