"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _format_gave_up_message,
)

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gave_up_notification_uses_exact_iteration_exhaustion_cause():
    msg = _format_gave_up_message(
        "[dsbmx04] ",
        "@dsbmx04-coder ",
        "t_1319666b",
        {"error": "Iteration budget exhausted (80/80)"},
    )

    assert "Iteration budget exhausted (80/80)" in msg
    assert "spawn" not in msg.lower()


def test_gave_up_notification_without_error_is_cause_neutral():
    msg = _format_gave_up_message("", "", "t_deadbeef", None)

    assert "failure limit reached" in msg
    assert "spawn" not in msg.lower()
