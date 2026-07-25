"""Regression tests for independent kanban notifier/dispatcher ownership."""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: MagicMock()}
    runner._kanban_sub_fail_counts = {}
    return runner


def test_non_owner_notifier_does_not_poll_board_dbs(monkeypatch):
    """A gateway that loses the notifier lease must not fan out over boards."""
    runner = _make_runner()
    sleep_calls = []

    async def _stop_after_lease_retry(delay):
        sleep_calls.append(delay)
        if delay != 5:
            runner._running = False

    with (
        patch(
            "gateway.kanban_watchers._acquire_singleton_lock",
            return_value=(None, "contended"),
        ) as acquire_lock,
        patch("hermes_cli.kanban_db.list_boards") as list_boards,
        patch("hermes_cli.kanban_db.connect") as connect,
        patch(
            "gateway.kanban_watchers.asyncio.sleep",
            side_effect=_stop_after_lease_retry,
        ),
    ):
        asyncio.run(runner._kanban_notifier_watcher(interval=0.1))

    acquire_lock.assert_called_once()
    list_boards.assert_not_called()
    connect.assert_not_called()
    assert sleep_calls == [5, 0.1]


def test_notifier_watcher_ignores_dispatch_disabled_env(monkeypatch):
    """The dispatcher env override must not disable notification delivery."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    runner = _make_runner()
    sleep_calls = []

    async def _stop_after_start(delay):
        sleep_calls.append(delay)
        runner._running = False

    with patch("gateway.kanban_watchers.asyncio.sleep", side_effect=_stop_after_start):
        asyncio.run(runner._kanban_notifier_watcher())

    assert sleep_calls == [5]


def test_notifier_watcher_does_not_read_dispatcher_config():
    """Notifier startup must be independent from dispatcher config loading."""
    runner = _make_runner()

    async def _stop_after_start(_delay):
        runner._running = False

    with (
        patch(
            "hermes_cli.config.load_config",
            side_effect=AssertionError("notifier must not read dispatcher config"),
        ),
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=_stop_after_start),
    ):
        asyncio.run(runner._kanban_notifier_watcher())


def test_dispatcher_watcher_remains_disabled_by_config(monkeypatch):
    """Decoupling the notifier must not enable embedded task dispatch."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    runner = _make_runner()

    with (
        patch(
            "hermes_cli.config.load_config",
            return_value={"kanban": {"dispatch_in_gateway": False}},
        ),
        patch("hermes_cli.kanban_db.dispatch_once") as dispatch_once,
    ):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=2))

    dispatch_once.assert_not_called()
