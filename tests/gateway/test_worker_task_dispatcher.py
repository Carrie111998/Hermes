from __future__ import annotations

import inspect

from gateway.worker_task_dispatcher import (
    GatewayWorkerTaskDispatcherMixin,
    _settings,
)


def test_auto_dispatch_defaults_on_and_is_configurable():
    assert _settings({}) == (True, 5.0, 3)
    assert _settings({"worker_bridge": {"auto_dispatch": False}})[0] is False
    assert _settings({"worker_bridge": {"auto_dispatch": {
        "enabled": True, "interval_seconds": 2, "per_tick": 7,
    }}}) == (True, 2.0, 7)


def test_gateway_runner_owns_supervised_worker_dispatcher():
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayWorkerTaskDispatcherMixin)
    assert inspect.iscoroutinefunction(
        GatewayWorkerTaskDispatcherMixin._worker_task_dispatcher_watcher
    )
