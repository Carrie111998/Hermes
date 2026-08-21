"""Detached subagents are active gateway work during restart/shutdown."""

import asyncio

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_drain_waits_for_detached_async_delegation(monkeypatch):
    from tools import async_delegation

    runner, _adapter = make_restart_runner()
    active = {"count": 1}
    monkeypatch.setattr(async_delegation, "active_count", lambda: active["count"])

    async def finish_delegation():
        await asyncio.sleep(0.12)
        active["count"] = 0

    task = asyncio.create_task(finish_delegation())
    _snapshot, timed_out = await runner._drain_active_agents(2.0)
    await task

    assert timed_out is False


def test_active_work_count_includes_detached_async_delegations(monkeypatch):
    from tools import async_delegation

    runner, _adapter = make_restart_runner()
    monkeypatch.setattr(async_delegation, "active_count", lambda: 3)
    monkeypatch.setattr(runner, "_running_agent_count", lambda: 1)
    monkeypatch.setattr(runner, "_active_cron_job_count", lambda: 2)
    monkeypatch.setattr(runner, "_active_api_run_count", lambda: 4)

    assert runner._active_work_count() == 10
