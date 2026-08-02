"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution as workflow
from hermes_cli import kanban_workflow_runtime as workflow_runtime
from hermes_cli import config as hermes_config

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_workflow_controller_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


@pytest.mark.parametrize(
    ("launcher_ready", "dispatch_enabled", "broker_ready", "status", "epoch_matches", "fresh", "expected"),
    [
        (False, True, True, "healthy", True, True, False),
        (True, False, True, "healthy", True, True, False),
        (True, True, False, "healthy", True, True, False),
        (True, True, True, "degraded", True, True, False),
        (True, True, True, "healthy", False, True, False),
        (True, True, True, "healthy", True, False, False),
        (True, True, True, "healthy", True, True, True),
    ],
)
def test_workflow_runtime_dispatch_gate_requires_every_authority(
    launcher_ready, dispatch_enabled, broker_ready, status, epoch_matches, fresh, expected
):
    now = int(time.time())
    state = SimpleNamespace(
        dispatch_enabled=dispatch_enabled,
        broker_ready=broker_ready,
        status=status,
        controller_epoch="epoch" if epoch_matches else "old",
        heartbeat_at=now if fresh else now - kb.WORKFLOW_CONTROLLER_STALE_SECONDS - 1,
    )
    assert kanban_watchers._workflow_runtime_dispatch_allowed(
        {
            "launcher_ready": launcher_ready,
            "worker_model": "test-model",
            "worker_provider": "test-provider",
        },
        state,
        controller_epoch="epoch",
        now=now,
    ) is expected


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


@pytest.mark.asyncio
async def test_workflow_controller_ticks_when_generic_dispatch_is_disabled(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=kb.DEFAULT_BOARD)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": False,
                "workflow": {
                    "enabled": True,
                    "launcher_ready": True,
                    "reconcile_interval_seconds": 1,
                },
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda *, include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )
    lock_handle = object()
    released = []
    monkeypatch.setattr(
        kanban_watchers,
        "_acquire_singleton_lock",
        lambda _path: (lock_handle, "held"),
    )
    monkeypatch.setattr(
        kanban_watchers,
        "_release_singleton_lock",
        lambda handle: released.append(handle),
    )

    runtime_ticks = []
    tick_order = []
    original_controller_tick = workflow.run_workflow_controller_tick

    def tracked_controller_tick(*args, **kwargs):
        tick_order.append("controller")
        return original_controller_tick(*args, **kwargs)

    monkeypatch.setattr(
        workflow, "run_workflow_controller_tick", tracked_controller_tick
    )
    monkeypatch.setattr(
        workflow_runtime,
        "run_workflow_runtime_tick",
        lambda *args, **kwargs: (
            tick_order.append("runtime"),
            runtime_ticks.append((args, kwargs)),
        ),
    )
    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    sleeps = 0

    async def stop_after_second_tick(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            runner._running = False

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", stop_after_second_tick)

    await runner._workflow_controller_watcher()

    with kb.connect(board=kb.DEFAULT_BOARD) as conn:
        state = workflow.get_workflow_controller_state(conn)
        assert state.status == "healthy"
        assert state.controller_epoch is not None
        assert state.controller_epoch.startswith("gateway:")
        assert state.last_reconciled_at is not None
        assert state.dispatch_enabled is False
    assert tick_order == ["controller", "runtime", "runtime", "controller"]
    assert len(runtime_ticks) == 2
    assert runtime_ticks[0][1]["launch_enabled"] is False
    assert isinstance(
        runtime_ticks[0][1]["coordinator"], workflow_runtime.WorkflowProductionCoordinator
    )
    assert runtime_ticks[0][1]["coordinator"].adapter.enabled is False
    assert released == [lock_handle]


@pytest.mark.asyncio
async def test_workflow_controller_calls_runtime_once_when_all_gates_are_true(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=kb.DEFAULT_BOARD)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "workflow": {
                    "enabled": True,
                    "launcher_ready": True,
                    "worker_model": "test-model",
                    "worker_provider": "test-provider",
                }
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda *, include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )
    lock_handle = object()
    monkeypatch.setattr(
        kanban_watchers, "_acquire_singleton_lock", lambda _path: (lock_handle, "held")
    )
    monkeypatch.setattr(kanban_watchers, "_release_singleton_lock", lambda _handle: None)

    def healthy_tick(conn, *, controller_epoch, **_kwargs):
        now = int(time.time())
        conn.execute(
            "UPDATE workflow_controller_state SET status='healthy', dispatch_enabled=1, "
            "broker_ready=1, controller_epoch=?, heartbeat_at=? WHERE singleton=1",
            (controller_epoch, now),
        )
        conn.commit()
        return workflow.ReconciliationReport((), (), (), ())

    monkeypatch.setattr(workflow, "run_workflow_controller_tick", healthy_tick)
    runtime_ticks = []
    monkeypatch.setattr(
        workflow_runtime,
        "run_workflow_runtime_tick",
        lambda *args, **kwargs: runtime_ticks.append((args, kwargs)),
    )
    runner = GatewayKanbanWatchersMixin()
    runner._running = True

    async def stop_after_first_tick(_interval):
        runner._running = False

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", stop_after_first_tick)
    await runner._workflow_controller_watcher()
    assert len(runtime_ticks) == 1
    assert runtime_ticks[0][1]["controller_epoch"].startswith("gateway:")
    assert runtime_ticks[0][1]["launch_enabled"] is True
    assert isinstance(runtime_ticks[0][1]["launcher"], workflow_runtime.HermesWorkflowLauncher)
    assert isinstance(
        runtime_ticks[0][1]["coordinator"], workflow_runtime.WorkflowProductionCoordinator
    )


@pytest.mark.asyncio
async def test_workflow_controller_standby_retries_lock_and_takes_over(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=kb.DEFAULT_BOARD)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "workflow": {
                    "enabled": True,
                    "reconcile_interval_seconds": 1,
                }
            }
        },
    )
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda *, include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )
    lock_handle = object()
    lock_attempts = iter([(None, "contended"), (lock_handle, "held")])
    monkeypatch.setattr(
        kanban_watchers,
        "_acquire_singleton_lock",
        lambda _path: next(lock_attempts),
    )
    released = []
    monkeypatch.setattr(
        kanban_watchers,
        "_release_singleton_lock",
        lambda handle: released.append(handle),
    )

    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    sleeps = []

    async def stop_after_takeover(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            runner._running = False

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", stop_after_takeover)

    await runner._workflow_controller_watcher()

    assert sleeps == [1.0, 1.0]
    assert released == [lock_handle]
    with kb.connect(board=kb.DEFAULT_BOARD) as conn:
        assert workflow.get_workflow_controller_state(conn).status == "healthy"


@pytest.mark.asyncio
async def test_workflow_controller_retries_transient_config_failure(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=kb.DEFAULT_BOARD)
    config_attempts = 0

    def load_config():
        nonlocal config_attempts
        config_attempts += 1
        if config_attempts == 1:
            raise OSError("temporary config read failure")
        return {
            "kanban": {
                "workflow": {
                    "enabled": True,
                    "reconcile_interval_seconds": 1,
                }
            }
        }

    monkeypatch.setattr(hermes_config, "load_config", load_config)
    monkeypatch.setattr(
        kb,
        "list_boards",
        lambda *, include_archived=False: [{"slug": kb.DEFAULT_BOARD}],
    )
    lock_handle = object()
    monkeypatch.setattr(
        kanban_watchers,
        "_acquire_singleton_lock",
        lambda _path: (lock_handle, "held"),
    )
    monkeypatch.setattr(kanban_watchers, "_release_singleton_lock", lambda _handle: None)

    runner = GatewayKanbanWatchersMixin()
    runner._running = True
    sleeps = []

    async def stop_after_retry_and_tick(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            runner._running = False

    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", stop_after_retry_and_tick)

    await runner._workflow_controller_watcher()

    assert config_attempts == 2
    assert sleeps == [1.0, 1.0]
    with kb.connect(board=kb.DEFAULT_BOARD) as conn:
        assert workflow.get_workflow_controller_state(conn).status == "healthy"
