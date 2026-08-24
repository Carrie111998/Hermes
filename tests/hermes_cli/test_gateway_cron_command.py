"""Gateway read-only /cron command coverage."""

import pytest

from hermes_cli.commands import gateway_help_lines, resolve_command
from hermes_cli.slash_exec import CommandContext, execute_command


def test_cron_is_gateway_available_read_only():
    cmd = resolve_command("cron")
    assert cmd is not None
    assert cmd.cli_only is False
    assert cmd.execute == "gateway_cron"
    assert any(
        "`/cron" in line and "read-only" in line.lower()
        for line in gateway_help_lines()
    )


def test_gateway_cron_list_includes_last_run_and_delivery(monkeypatch):
    calls = []

    def fake_list_jobs(*, include_disabled=False):
        calls.append(include_disabled)
        return [
            {
                "id": "abc123",
                "name": "daily report",
                "enabled": True,
                "state": "scheduled",
                "schedule_display": "15 10 * * *",
                "next_run_at": "2026-08-25T10:15:00+08:00",
                "last_run_at": "2026-08-24T10:17:29+08:00",
                "last_status": "ok",
                "last_delivery_error": "delivery endpoint unavailable",
                "no_agent": False,
                "workdir": "/srv/reports",
            },
            {
                "id": "watchdog",
                "name": "script watchdog",
                "enabled": True,
                "state": "scheduled",
                "schedule_display": "every 5m",
                "next_run_at": "2026-08-25T10:20:00+08:00",
                "last_run_at": None,
                "last_status": None,
                "script": "watch.py",
                "no_agent": True,
            },
        ]

    monkeypatch.setattr("cron.jobs.list_jobs", fake_list_jobs)
    reply = execute_command("cron", CommandContext(surface="gateway", args="list"))

    assert calls == [False]
    assert "Cron jobs (2 active)" in reply.text
    assert "daily report" in reply.text
    assert "2026-08-24T10:17:29+08:00 ok" in reply.text
    assert "delivery_error: delivery endpoint unavailable" in reply.text
    assert "script-only jobs can be intentionally silent" in reply.text
    assert reply.data == {"count": 2, "include_disabled": False}


def test_gateway_cron_list_all_passes_include_disabled(monkeypatch):
    calls = []

    def fake_list_jobs(*, include_disabled=False):
        calls.append(include_disabled)
        return []

    monkeypatch.setattr("cron.jobs.list_jobs", fake_list_jobs)
    reply = execute_command("cron", CommandContext(surface="gateway", args="list --all"))

    assert calls == [True]
    assert "No scheduled cron jobs found" in reply.text
    assert reply.data == {"count": 0, "include_disabled": True}


@pytest.mark.parametrize(
    "subcommand",
    ["add", "create", "edit", "pause", "resume", "run", "remove"],
)
def test_gateway_cron_blocks_mutating_subcommands(monkeypatch, subcommand):
    def fail_list_jobs(*, include_disabled=False):  # pragma: no cover - must not run
        raise AssertionError("mutating subcommands must not list jobs")

    monkeypatch.setattr("cron.jobs.list_jobs", fail_list_jobs)
    reply = execute_command(
        "cron",
        CommandContext(surface="gateway", args=f"{subcommand} abc123"),
    )

    assert "read-only" in reply.text
    assert "add/create/edit/pause/resume/run/remove" in reply.text
    assert reply.data == {"blocked_subcommand": subcommand}
