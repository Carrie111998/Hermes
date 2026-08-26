"""Gateway /cron read-only handler coverage."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli.commands import resolve_command, should_bypass_active_session


def _active_job():
    return {
        "id": "job123",
        "name": "daily report",
        "enabled": True,
        "state": "scheduled",
        "schedule_display": "0 9 * * *",
        "next_run_at": "2026-08-25T09:00:00+08:00",
        "last_run_at": "2026-08-24T09:00:00+08:00",
        "last_status": "ok",
    }


def test_gateway_cron_command_lists_jobs(monkeypatch):
    def fake_list_jobs(*, include_disabled=False):
        assert include_disabled is False
        return [_active_job()]

    monkeypatch.setattr("cron.jobs.list_jobs", fake_list_jobs)
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    event = SimpleNamespace(get_command_args=lambda: "list")

    text = asyncio.run(runner._handle_cron_command(event))

    assert "Cron jobs (1 active)" in text
    assert "daily report" in text
    assert "last: 2026-08-24T09:00:00+08:00 ok" in text


def test_gateway_cron_command_uses_source_profile(tmp_path, monkeypatch):
    from cron.jobs import save_jobs, use_cron_store

    default_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "research"
    default_home.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    default_job = {**_active_job(), "id": "default-job", "name": "default only"}
    profile_job = {**_active_job(), "id": "profile-job", "name": "research only"}
    with use_cron_store(default_home):
        save_jobs([default_job], replace=True)
    with use_cron_store(profile_home):
        save_jobs([profile_job], replace=True)
    profile_jobs_file = profile_home / "cron" / "jobs.json"
    jobs_before = profile_jobs_file.read_bytes()

    runner = object.__new__(GatewaySlashCommandsMixin)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda source: profile_home
    event = SimpleNamespace(
        source=SimpleNamespace(profile="research"),
        get_command_args=lambda: "list",
    )

    text = asyncio.run(runner._handle_cron_command(event))

    assert "research only" in text
    assert "default only" not in text
    assert profile_jobs_file.read_bytes() == jobs_before


def test_gateway_cron_command_dispatches_while_agent_is_busy():
    from gateway.run import GatewayRunner

    command = resolve_command("cron")
    assert command is not None
    assert command.busy_policy == "dispatch"
    assert should_bypass_active_session("cron") is True

    runner = object.__new__(GatewayRunner)
    runner._handle_cron_command = AsyncMock(return_value="Cron jobs (1 active)")
    event = SimpleNamespace()

    result = asyncio.run(
        runner._dispatch_busy_slash_command(event, command, "session", event)
    )

    assert result == "Cron jobs (1 active)"
    runner._handle_cron_command.assert_awaited_once_with(event)
