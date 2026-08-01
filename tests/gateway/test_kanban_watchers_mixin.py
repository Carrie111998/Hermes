"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

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


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


class TestDispatcherExceptionPropagation:
    """Regression: dispatcher watcher must re-raise exceptions so that
    _spawn_supervised sees the exception and restarts the watcher.
    A clean return defeats the supervisor — see PR #72434 follow-up."""

    @pytest.mark.asyncio
    async def test_outer_handler_re_raises_exception(self):
        """When _kanban_dispatcher_loop raises, _kanban_dispatcher_watcher
        must propagate the exception (not swallow it with a clean return)."""
        from unittest.mock import MagicMock, patch

        from gateway.kanban_watchers import GatewayKanbanWatchersMixin

        mixin = MagicMock()
        mixin._kanban_dispatcher_lock_handle = MagicMock()

        async def fake_loop(_kb, kanban_cfg):
            raise RuntimeError("simulated dispatcher crash")

        mixin._kanban_dispatcher_loop = fake_loop

        with patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(MagicMock(), "held"),
        ), patch(
            "gateway.kanban_watchers._release_singleton_lock",
        ), patch.dict(
            "sys.modules",
            {"hermes_cli": MagicMock(kanban_db=MagicMock())},
        ):
            with pytest.raises(RuntimeError, match="simulated dispatcher crash"):
                await GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(mixin)

        # Verify the lock handle was cleared before re-raising
        assert mixin._kanban_dispatcher_lock_handle is None
    @pytest.mark.asyncio
    async def test_cancelled_error_releases_lock_and_reraises(self):
        """When the dispatcher watcher is cancelled during _kanban_dispatcher_loop,
        it must release the singleton lock and re-raise CancelledError so
        _spawn_supervised can handle cancellation properly."""
        from unittest.mock import MagicMock, patch
        import asyncio

        from gateway.kanban_watchers import GatewayKanbanWatchersMixin

        mixin = MagicMock()
        mixin._kanban_dispatcher_lock_handle = MagicMock()

        async def fake_loop(_kb, kanban_cfg):
            raise asyncio.CancelledError("dispatcher cancelled")

        mixin._kanban_dispatcher_loop = fake_loop

        with patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(MagicMock(), "held"),
        ), patch(
            "gateway.kanban_watchers._release_singleton_lock",
        ) as mock_release, patch.dict(
            "sys.modules",
            {"hermes_cli": MagicMock(kanban_db=MagicMock())},
        ):
            with pytest.raises(asyncio.CancelledError, match="dispatcher cancelled"):
                await GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(mixin)

        # Verify lock was released and handle cleared
        mock_release.assert_called_once()
        assert mixin._kanban_dispatcher_lock_handle is None

    @pytest.mark.asyncio
    async def test_lock_release_idempotent_when_none(self):
        """When _kanban_dispatcher_loop raises and _kanban_dispatcher_lock_handle
        is already None (lock never acquired or already released),
        the exception handler must still re-raise without crashing."""
        from unittest.mock import MagicMock, patch

        from gateway.kanban_watchers import GatewayKanbanWatchersMixin

        mixin = MagicMock()
        mixin._kanban_dispatcher_lock_handle = None  # Already None

        async def fake_loop(_kb, kanban_cfg):
            raise RuntimeError("crash before lock acquired")

        mixin._kanban_dispatcher_loop = fake_loop

        with patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(MagicMock(), "held"),
        ), patch(
            "gateway.kanban_watchers._release_singleton_lock",
        ) as mock_release, patch.dict(
            "sys.modules",
            {"hermes_cli": MagicMock(kanban_db=MagicMock())},
        ):
            with pytest.raises(RuntimeError, match="crash before lock acquired"):
                await GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(mixin)

        # _release_singleton_lock should still be called (with None)
        mock_release.assert_called_once_with(None)
