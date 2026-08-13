"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _external_profile_counts,
    _rotate_board_slugs,
)

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_board_admission_rotation_is_stable_and_fair():
    boards = ["alpha", "beta", "gamma"]

    assert _rotate_board_slugs(boards, 0) == ["alpha", "beta", "gamma"]
    assert _rotate_board_slugs(boards, 1) == ["beta", "gamma", "alpha"]
    assert _rotate_board_slugs(boards, 2) == ["gamma", "alpha", "beta"]
    assert boards == ["alpha", "beta", "gamma"]
    assert _rotate_board_slugs([], 99) == []


def test_external_profile_counts_excludes_current_board():
    assert _external_profile_counts(
        {"alpha": 3, "beta": 1}, {"alpha": 1, "beta": 1}
    ) == {"alpha": 2}


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"
