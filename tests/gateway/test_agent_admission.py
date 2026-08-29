"""Regression coverage for bounded, resource-aware gateway admission."""

from __future__ import annotations

import asyncio

import pytest

from gateway.admission import (
    AdmissionRejected,
    AgentAdmissionController,
    cgroup_available_memory_mb,
)


def test_cgroup_headroom_uses_memory_max_minus_current(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/system.slice/hermes.service\n", encoding="utf-8")
    cgroup_dir = tmp_path / "root" / "system.slice" / "hermes.service"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "memory.max").write_text(str(3 * 1024 * 1024 * 1024), encoding="utf-8")
    (cgroup_dir / "memory.current").write_text(str(2 * 1024 * 1024 * 1024), encoding="utf-8")

    assert cgroup_available_memory_mb(cgroup_file, tmp_path / "root") == 1024


def test_unbounded_cgroup_defers_to_host_headroom(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/user.slice/test\n", encoding="utf-8")
    cgroup_dir = tmp_path / "root" / "user.slice" / "test"
    cgroup_dir.mkdir(parents=True)
    (cgroup_dir / "memory.max").write_text("max", encoding="utf-8")
    (cgroup_dir / "memory.current").write_text("123", encoding="utf-8")

    assert cgroup_available_memory_mb(cgroup_file, tmp_path / "root") is None


@pytest.mark.asyncio
async def test_parallel_capacity_queues_and_starts_fifo_after_release():
    notices: list[tuple[str, str]] = []
    controller = AgentAdmissionController(
        max_parallel=2, queue_limit=3, poll_interval_seconds=0.01
    )
    await controller.acquire("agent-1")
    await controller.acquire("agent-2")

    async def queued(task_id: str):
        await controller.acquire(
            task_id, on_queued=lambda text: _record(notices, task_id, text)
        )

    third = asyncio.create_task(queued("agent-3"))
    fourth = asyncio.create_task(queued("agent-4"))
    await asyncio.sleep(0.03)
    assert controller.snapshot().active_task_ids == ("agent-1", "agent-2")
    assert controller.snapshot().queued_task_ids == ("agent-3", "agent-4")
    assert "parallel-agent capacity (2/2)" in notices[0][1]

    await controller.release("agent-1")
    await asyncio.wait_for(third, timeout=0.2)
    assert "agent-3" in controller.snapshot().active_task_ids
    assert not fourth.done()

    await controller.release("agent-2")
    await asyncio.wait_for(fourth, timeout=0.2)


async def _record(rows: list[tuple[str, str]], task_id: str, text: str) -> None:
    rows.append((task_id, text))


@pytest.mark.asyncio
async def test_memory_pressure_queues_without_interrupting_running_agent():
    available = [500]
    controller = AgentAdmissionController(
        max_parallel=3,
        min_headroom_mb=1024,
        queue_limit=2,
        poll_interval_seconds=0.01,
        memory_reader=lambda: available[0],
    )
    waiter = asyncio.create_task(controller.acquire("memory-waiter"))
    await asyncio.sleep(0.03)
    assert controller.snapshot().active == 0
    assert controller.snapshot().queued == 1
    available[0] = 2048
    await asyncio.wait_for(waiter, timeout=0.2)
    assert controller.snapshot().active_task_ids == ("memory-waiter",)


@pytest.mark.asyncio
async def test_worker_crash_releases_only_its_slot_and_queue_continues():
    controller = AgentAdmissionController(
        max_parallel=2, queue_limit=2, poll_interval_seconds=0.01
    )
    await controller.acquire("healthy")
    await controller.acquire("crashing")
    queued = asyncio.create_task(controller.acquire("next"))
    await asyncio.sleep(0.02)

    # The gateway's handler finally block uses this same outcome-independent
    # release path when an agent raises or its worker is OOM-killed.
    await controller.release("crashing", outcome="crash")
    await asyncio.wait_for(queued, timeout=0.2)
    assert set(controller.snapshot().active_task_ids) == {"healthy", "next"}


@pytest.mark.asyncio
async def test_queue_limit_and_restart_reconciliation_are_explicit():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=1, poll_interval_seconds=0.01
    )
    await controller.acquire("active")
    queued = asyncio.create_task(controller.acquire("queued"))
    await asyncio.sleep(0.02)
    with pytest.raises(AdmissionRejected, match="queue is full"):
        await controller.acquire("overflow")

    reconciled = await controller.close("restart reconciliation")
    assert reconciled == ("queued",)
    with pytest.raises(AdmissionRejected, match="restart reconciliation"):
        await queued
    assert controller.snapshot().active_task_ids == ("active",)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_block_following_task():
    controller = AgentAdmissionController(
        max_parallel=1, queue_limit=2, poll_interval_seconds=0.01
    )
    await controller.acquire("active")
    cancelled = asyncio.create_task(controller.acquire("cancelled"))
    following = asyncio.create_task(controller.acquire("following"))
    await asyncio.sleep(0.02)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await controller.release("active")
    await asyncio.wait_for(following, timeout=0.2)
    assert controller.snapshot().active_task_ids == ("following",)
