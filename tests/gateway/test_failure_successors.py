from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

import gateway.failure_successors as failure_successors_module
import gateway.worker_bridge_watchers as worker_bridge_watchers
from gateway.failure_successors import (
    create_failure_successors,
    resolve_failure_successor_settings,
)
from gateway.worker_bridge_watchers import GatewayWorkerBridgeWatchersMixin


@pytest.fixture(autouse=True)
def _clear_reported_skips():
    failure_successors_module._reported_skips.clear()
    yield
    failure_successors_module._reported_skips.clear()


class FakeBridge:
    def __init__(self, tasks: list[dict]):
        self.tasks = deepcopy(tasks)
        self.created_specs: list[dict] = []

    def list_tasks(self, **_kwargs):
        return deepcopy(self.tasks)

    def create_task(self, spec):
        captured = deepcopy(spec)
        self.created_specs.append(captured)
        created = {
            "task_id": f"successor-{len(self.created_specs)}",
            "status": "created",
            "spec": captured,
            "runtime": {},
            "result": None,
        }
        self.tasks.append(created)
        return deepcopy(created)


def _task(
    *,
    task_id: str = "failed-1",
    status: str = "failed",
    depth: int = 0,
    auto_repair: int = 1,
    repair_attempts: int = 1,
    error: str = "tests still fail",
    parent_task_id: str | None = None,
    failure_successor: bool = False,
) -> dict:
    metadata = {
        "auto_repair": auto_repair,
        "successor_chain_depth": depth,
    }
    if failure_successor:
        metadata["failure_successor"] = True
    return {
        "task_id": task_id,
        "status": status,
        "spec": {
            "task_id": task_id,
            "objective": "Fix the broken behavior",
            "worker": "codex",
            "workspace": {
                "repository": "C:/repo",
                "isolation": "shared",
            },
            "parent_task_id": parent_task_id,
            "metadata": metadata,
        },
        "runtime": {"auto_repair_attempts": repair_attempts},
        "result": {"error": error},
    }


def _settings(**overrides) -> dict:
    return {"enabled": True, "max_chain": 2, **overrides}


@pytest.mark.asyncio
async def test_one_notifier_watcher_tick_runs_failure_successor_pass_once(monkeypatch):
    bridge = FakeBridge([_task()])
    runner = GatewayWorkerBridgeWatchersMixin()
    runner._get_worker_bridge = lambda _db_path: (bridge, None)
    runner._running = True
    calls = 0
    real_create = create_failure_successors

    def counting_create(target_bridge, settings):
        nonlocal calls
        calls += 1
        return real_create(target_bridge, settings)

    monkeypatch.setattr(
        worker_bridge_watchers,
        "create_failure_successor_tasks",
        counting_create,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"worker_bridge": {"failure_successors": _settings()}},
    )

    async def stop_after_tick(_delay):
        runner._running = False

    monkeypatch.setattr(worker_bridge_watchers.asyncio, "sleep", stop_after_tick)

    await runner._worker_bridge_notifier_watcher(interval=0.01)

    assert calls == 1
    assert len(bridge.created_specs) == 1


@pytest.mark.asyncio
async def test_recursive_watcher_failure_successor_entry_is_ignored(monkeypatch):
    bridge = FakeBridge([_task()])
    runner = GatewayWorkerBridgeWatchersMixin()
    runner._get_worker_bridge = lambda _db_path: (bridge, None)
    config = {"worker_bridge": {"failure_successors": _settings()}}
    loop = asyncio.get_running_loop()
    nested_results = []

    def recursively_trigger_entry(target_bridge, settings):
        nested = asyncio.run_coroutine_threadsafe(
            runner._worker_bridge_failure_successors(
                config,
                db_path=Path("bridge.db"),
            ),
            loop,
        )
        nested_results.append(nested.result(timeout=1))
        return create_failure_successors(target_bridge, settings)

    monkeypatch.setattr(
        worker_bridge_watchers,
        "create_failure_successor_tasks",
        recursively_trigger_entry,
    )

    created = await runner._worker_bridge_failure_successors(
        config,
        db_path=Path("bridge.db"),
    )

    assert created == 1
    assert nested_results == [0]
    assert len(bridge.created_specs) == 1


@pytest.mark.asyncio
async def test_watcher_failure_successor_guard_resets_after_exception(monkeypatch):
    bridge = FakeBridge([_task()])
    runner = GatewayWorkerBridgeWatchersMixin()
    runner._get_worker_bridge = lambda _db_path: (bridge, None)
    config = {"worker_bridge": {"failure_successors": _settings()}}

    def fail_once(_bridge, _settings):
        raise RuntimeError("successor policy failed")

    monkeypatch.setattr(
        worker_bridge_watchers,
        "create_failure_successor_tasks",
        fail_once,
    )
    with pytest.raises(RuntimeError, match="successor policy failed"):
        await runner._worker_bridge_failure_successors(
            config,
            db_path=Path("bridge.db"),
        )

    assert runner._failure_successor_pass_running is False

    monkeypatch.setattr(
        worker_bridge_watchers,
        "create_failure_successor_tasks",
        create_failure_successors,
    )
    assert await runner._worker_bridge_failure_successors(
        config,
        db_path=Path("bridge.db"),
    ) == 1


@pytest.mark.asyncio
async def test_watcher_pass_preserves_max_chain_and_idempotency():
    runner = GatewayWorkerBridgeWatchersMixin()
    capped_bridge = FakeBridge([_task(depth=2)])
    runner._get_worker_bridge = lambda _db_path: (capped_bridge, None)
    config = {"worker_bridge": {"failure_successors": _settings(max_chain=2)}}

    assert await runner._worker_bridge_failure_successors(
        config,
        db_path=Path("bridge.db"),
    ) == 0
    assert capped_bridge.created_specs == []

    replay_bridge = FakeBridge([_task()])
    runner._get_worker_bridge = lambda _db_path: (replay_bridge, None)
    assert await runner._worker_bridge_failure_successors(
        config,
        db_path=Path("bridge.db"),
    ) == 1
    assert await runner._worker_bridge_failure_successors(
        config,
        db_path=Path("bridge.db"),
    ) == 0
    assert len(replay_bridge.created_specs) == 1


def test_creates_one_successor_after_auto_repair_is_exhausted():
    bridge = FakeBridge([_task()])

    assert create_failure_successors(bridge, _settings()) == 1
    assert len(bridge.created_specs) == 1
    successor = bridge.created_specs[0]
    assert successor["parent_task_id"] == "failed-1"
    assert successor["idempotency_key"] == "failure-successor:failed-1"
    assert successor["metadata"]["failure_successor"] is True
    assert successor["metadata"]["successor_chain_depth"] == 1
    assert successor["objective"].startswith("Fix the broken behavior")
    assert "tests still fail" in successor["objective"]


def test_max_chain_stops_successor_creation():
    bridge = FakeBridge([_task(depth=2)])

    assert create_failure_successors(bridge, _settings(max_chain=2)) == 0
    assert bridge.created_specs == []


def test_cancelled_task_is_excluded():
    bridge = FakeBridge([_task(status="cancelled")])

    assert create_failure_successors(bridge, _settings()) == 0


def test_replay_is_idempotent():
    bridge = FakeBridge([_task()])

    assert create_failure_successors(bridge, _settings()) == 1
    assert create_failure_successors(bridge, _settings()) == 0
    assert len(bridge.created_specs) == 1


def test_existing_live_successor_is_excluded():
    parent = _task()
    child = _task(
        task_id="successor-live",
        status="running",
        depth=1,
        parent_task_id="failed-1",
        failure_successor=True,
    )
    bridge = FakeBridge([parent, child])

    assert create_failure_successors(bridge, _settings()) == 0


def test_environmental_timeout_is_excluded():
    bridge = FakeBridge(
        [_task(status="timed_out", error="'codex' is not recognized")]
    )

    assert create_failure_successors(bridge, _settings()) == 0


def test_config_off_does_not_create():
    bridge = FakeBridge([_task()])
    settings = resolve_failure_successor_settings(
        {"worker_bridge": {"failure_successors": {"enabled": False}}}
    )

    assert create_failure_successors(bridge, settings) == 0
    assert bridge.created_specs == []


def test_config_defaults_are_enabled_and_bounded_to_two():
    assert resolve_failure_successor_settings({}) == {
        "enabled": True,
        "max_chain": 2,
    }


def test_task_still_in_auto_repair_is_excluded():
    bridge = FakeBridge([_task(auto_repair=2, repair_attempts=1)])

    assert create_failure_successors(bridge, _settings()) == 0


class RejectingBridge(FakeBridge):
    """Rejects creation for one parent, e.g. its workspace was deleted."""

    def __init__(self, tasks: list[dict], reject_parent: str):
        super().__init__(tasks)
        self.reject_parent = reject_parent

    def create_task(self, spec):
        if spec.get("parent_task_id") == self.reject_parent:
            raise ValueError("repository does not exist: C:/gone")
        return super().create_task(spec)


def test_unservable_parent_does_not_abort_pass_for_other_tasks(caplog):
    bridge = RejectingBridge(
        [_task(task_id="dead-repo"), _task(task_id="failed-2")],
        reject_parent="dead-repo",
    )

    with caplog.at_level("WARNING", logger="gateway.run"):
        assert create_failure_successors(bridge, _settings()) == 1

    assert len(bridge.created_specs) == 1
    assert bridge.created_specs[0]["parent_task_id"] == "failed-2"
    skips = [r for r in caplog.records if "successor skipped" in r.getMessage()]
    assert len(skips) == 1 and "dead-repo" in skips[0].getMessage()


def test_unservable_parent_warns_once_across_passes(caplog):
    bridge = RejectingBridge([_task(task_id="dead-repo")], reject_parent="dead-repo")

    with caplog.at_level("WARNING", logger="gateway.run"):
        assert create_failure_successors(bridge, _settings()) == 0
        assert create_failure_successors(bridge, _settings()) == 0

    skips = [r for r in caplog.records if "successor skipped" in r.getMessage()]
    assert len(skips) == 1
