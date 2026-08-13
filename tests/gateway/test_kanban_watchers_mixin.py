"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio
import inspect
import logging

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


def test_cross_board_census_failure_fails_closed_for_dispatch_tick(
    tmp_path, monkeypatch, caplog
):
    import hermes_cli.config as config_module
    import hermes_cli.kanban_db as kb_module
    import gateway.kanban_watchers as watchers_module
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._authorization_adapter = lambda platform, profile=None: None
    dispatch_calls: list[str] = []

    class CensusConnection:
        def execute(self, query):
            return self

        def fetchall(self):
            return []

        def close(self):
            return None

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "max_in_progress_per_profile": 1,
                "alerts": {"enabled": False},
            }
        },
    )
    monkeypatch.setattr(
        watchers_module, "_acquire_singleton_lock", lambda path: (None, "unavailable")
    )
    monkeypatch.setattr(kb_module, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(
        kb_module,
        "list_boards",
        lambda include_archived=False: [{"slug": "alpha"}, {"slug": "broken"}],
    )

    def connect(*, board):
        if board == "broken":
            raise OSError("census unavailable")
        return CensusConnection()

    monkeypatch.setattr(kb_module, "connect", connect)
    monkeypatch.setattr(kb_module, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb_module, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb_module, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(
        kb_module,
        "dispatch_once",
        lambda conn, *, board, **kwargs: dispatch_calls.append(board),
    )

    async def immediate_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    sleep_calls = 0

    async def stop_after_tick(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)

    with caplog.at_level(logging.ERROR):
        asyncio.run(runner._kanban_dispatcher_watcher())

    assert dispatch_calls == []
    assert "cross-board running census failed" in caplog.text
    assert "spawning is disabled for this tick" in caplog.text
