from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import gateway.worker_bridge_watchers as watchers
from gateway.worker_bridge_watchers import GatewayWorkerBridgeWatchersMixin


@pytest.mark.asyncio
async def test_continuation_passes_run_when_alerts_are_disabled(monkeypatch):
    runner = GatewayWorkerBridgeWatchersMixin()
    runner._running = True
    calls: list[str] = []

    async def failure(_config, *, db_path):
        calls.append("failure")
        return 0

    async def review(_config, *, db_path):
        calls.append("review")
        return 0

    async def stage(_config, *, db_path):
        calls.append("stage")
        return 0

    runner._worker_bridge_failure_successors = failure
    runner._worker_bridge_review_continuations = review
    runner._worker_bridge_stage_successors = stage
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"worker_bridge": {"gateway_alerts": {"enabled": False}}},
    )

    async def stop_after_tick(_delay):
        runner._running = False

    monkeypatch.setattr(watchers.asyncio, "sleep", stop_after_tick)

    await runner._worker_bridge_notifier_watcher(interval=0.01)

    assert calls == ["failure", "review", "stage"]


@pytest.mark.parametrize(
    ("method_name", "function_name", "config_key", "guard_name"),
    [
        (
            "_worker_bridge_review_continuations",
            "create_review_continuation_tasks",
            "review_continuation",
            "_review_continuation_pass_running",
        ),
        (
            "_worker_bridge_stage_successors",
            "create_stage_successor_tasks",
            "stage_successors",
            "_stage_successor_pass_running",
        ),
    ],
)
@pytest.mark.asyncio
async def test_continuation_pass_reentry_is_ignored(
    monkeypatch,
    method_name,
    function_name,
    config_key,
    guard_name,
):
    runner = GatewayWorkerBridgeWatchersMixin()
    bridge = object()
    runner._get_worker_bridge = lambda _db_path: (bridge, None)
    config = {"worker_bridge": {config_key: {"enabled": True}}}
    loop = asyncio.get_running_loop()
    nested_results: list[int] = []

    def recursively_trigger(target_bridge, settings):
        assert target_bridge is bridge
        nested = asyncio.run_coroutine_threadsafe(
            getattr(runner, method_name)(config, db_path=Path("bridge.db")),
            loop,
        )
        nested_results.append(nested.result(timeout=1))
        return 1

    monkeypatch.setattr(watchers, function_name, recursively_trigger)

    assert (
        await getattr(runner, method_name)(config, db_path=Path("bridge.db"))
        == 1
    )
    assert nested_results == [0]
    assert getattr(runner, guard_name) is False


@pytest.mark.parametrize(
    ("method_name", "config_key"),
    [
        ("_worker_bridge_review_continuations", "review_continuation"),
        ("_worker_bridge_stage_successors", "stage_successors"),
    ],
)
@pytest.mark.asyncio
async def test_continuation_pass_bridge_unavailable_is_a_noop(
    method_name,
    config_key,
):
    runner = GatewayWorkerBridgeWatchersMixin()
    runner._get_worker_bridge = lambda _db_path: None
    config = {"worker_bridge": {config_key: {"enabled": True}}}

    assert (
        await getattr(runner, method_name)(config, db_path=Path("missing.db"))
        == 0
    )
