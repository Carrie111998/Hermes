"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_lease_next_notification",
    "_kanban_send_notification_with_lease_heartbeat",
    "_kanban_renew_notification_lease",
    "_kanban_ack_notification",
    "_kanban_fail_notification",
    "_kanban_unsub",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for method in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, method), (
            f"mixin missing {method}"
        )


def test_gateway_watchers_honor_the_dispatch_disabled_environment(monkeypatch):
    """Both background loops must exit before opening Kanban state when disabled."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")

    asyncio.run(runner._kanban_notifier_watcher())
    asyncio.run(runner._kanban_dispatcher_watcher())


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one dispatcher holder may own the machine-local advisory lock."""
    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    first_handle, first_state = _acquire_singleton_lock(lock)
    assert first_state == "held" and first_handle is not None

    second_handle, second_state = _acquire_singleton_lock(lock)
    assert second_state == "contended" and second_handle is None

    _release_singleton_lock(first_handle)
    third_handle, third_state = _acquire_singleton_lock(lock)
    assert third_state == "held" and third_handle is not None
    _release_singleton_lock(third_handle)
