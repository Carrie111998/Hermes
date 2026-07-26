from __future__ import annotations

import inspect

import gateway.run as gateway_run
from gateway.worker_bridge_watchers import GatewayWorkerBridgeWatchersMixin
from gateway.worker_task_dispatcher import GatewayWorkerTaskDispatcherMixin


def _scheduled_watchers(
    monkeypatch, *, alerts_enabled: bool, successors_enabled: bool = True
) -> list[tuple]:
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "worker_bridge": {
                "gateway_alerts": {"enabled": alerts_enabled},
                "failure_successors": {"enabled": successors_enabled},
            },
        },
    )
    runner = object.__new__(gateway_run.GatewayRunner)
    scheduled = []
    runner._spawn_supervised = (
        lambda coro_factory, name, **kwargs: scheduled.append(
            (coro_factory, name, kwargs)
        )
    )

    runner._start_worker_bridge_watchers()

    return scheduled


def test_gateway_runner_mro_wires_worker_bridge_before_created_task_dispatcher():
    mro = gateway_run.GatewayRunner.__mro__

    assert issubclass(gateway_run.GatewayRunner, GatewayWorkerBridgeWatchersMixin)
    assert mro.index(GatewayWorkerBridgeWatchersMixin) < mro.index(
        GatewayWorkerTaskDispatcherMixin
    )


def test_startup_schedules_each_worker_bridge_watcher_once_when_enabled(monkeypatch):
    scheduled = _scheduled_watchers(monkeypatch, alerts_enabled=True)

    assert [name for _, name, _ in scheduled] == [
        "worker_task_dispatcher_watcher",
        "worker_bridge_notifier_watcher",
    ]
    assert scheduled[0][0].__func__ is (
        GatewayWorkerTaskDispatcherMixin._worker_task_dispatcher_watcher
    )
    assert scheduled[1][0].__func__ is (
        GatewayWorkerBridgeWatchersMixin._worker_bridge_notifier_watcher
    )


def test_startup_does_not_schedule_worker_bridge_notifier_when_disabled(monkeypatch):
    scheduled = _scheduled_watchers(
        monkeypatch, alerts_enabled=False, successors_enabled=False
    )

    assert [name for _, name, _ in scheduled] == [
        "worker_task_dispatcher_watcher",
    ]
    assert scheduled[0][0].__func__ is (
        GatewayWorkerTaskDispatcherMixin._worker_task_dispatcher_watcher
    )


def test_gateway_start_calls_worker_bridge_startup_hook_once():
    start_source = inspect.getsource(gateway_run.GatewayRunner.start)

    assert start_source.count("self._start_worker_bridge_watchers()") == 1
