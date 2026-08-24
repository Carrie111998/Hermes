"""Gateway /cron read-only handler coverage."""

from types import SimpleNamespace

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


@pytest.mark.asyncio
async def test_gateway_cron_command_lists_jobs(monkeypatch):
    def fake_list_jobs(*, include_disabled=False):
        assert include_disabled is False
        return [
            {
                "id": "job123",
                "name": "daily report",
                "enabled": True,
                "state": "scheduled",
                "schedule_display": "0 9 * * *",
                "next_run_at": "2026-08-25T09:00:00+08:00",
                "last_run_at": "2026-08-24T09:00:00+08:00",
                "last_status": "ok",
            }
        ]

    monkeypatch.setattr("cron.jobs.list_jobs", fake_list_jobs)
    runner = object.__new__(GatewaySlashCommandsMixin)
    event = SimpleNamespace(get_command_args=lambda: "list")

    text = await runner._handle_cron_command(event)

    assert "Cron jobs (1 active)" in text
    assert "daily report" in text
    assert "last: 2026-08-24T09:00:00+08:00 ok" in text
