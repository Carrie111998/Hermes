"""Notifier polling is independent from the embedded-dispatcher gate."""

import asyncio
from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.run import GatewayRunner


def _make_runner(with_adapter=False):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_notifier_profile = "default"
    runner.adapters = {Platform.TELEGRAM: MagicMock()} if with_adapter else {}
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    return runner


def _fake_config(dispatch_in_gateway):
    return {"kanban": {"dispatch_in_gateway": dispatch_in_gateway}}


def test_notifier_watcher_runs_when_dispatch_disabled(tmp_path, monkeypatch):
    """Notifier-only gateways still poll without invoking dispatcher work."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    runner = _make_runner(with_adapter=True)
    runner.adapters[Platform.TELEGRAM].send = MagicMock()
    past_gate = []
    sleep_calls = []

    async def fake_sleep(_delay):
        sleep_calls.append(_delay)
        if len(sleep_calls) >= 2:
            runner._running = False

    with patch("hermes_cli.config.load_config", return_value=_fake_config(False)):
        with patch(
            "hermes_cli.kanban_db.list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ), patch("asyncio.sleep", side_effect=fake_sleep), patch(
            "hermes_cli.kanban_db.dispatch_once"
        ) as dispatch_once:
            asyncio.run(runner._kanban_notifier_watcher(interval=1))
    assert past_gate
    dispatch_once.assert_not_called()


def test_notifier_watcher_env_override_does_not_disable_notifications(tmp_path, monkeypatch):
    """The dispatcher env override cannot turn off notifier-only polling."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    runner = _make_runner(with_adapter=True)
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    calls = []
    sleeps = []

    async def fake_sleep(_delay):
        sleeps.append(_delay)
        if len(sleeps) >= 2:
            runner._running = False

    with patch(
        "hermes_cli.kanban_db.list_boards",
        side_effect=lambda *a, **kw: calls.append(True) or [],
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(runner._kanban_notifier_watcher(interval=1))
    assert calls


def test_notifier_watcher_runs_when_dispatch_enabled(tmp_path, monkeypatch):
    """dispatch_in_gateway=true proceeds past the gate to the board fan-out."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    runner = _make_runner(with_adapter=True)
    past_gate = []
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Stop after the initial delay + first per-interval sleep so the loop
        # body runs exactly once.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import hermes_cli.kanban_db as _kb

    with patch("hermes_cli.config.load_config", return_value=_fake_config(True)):
        with patch.object(
            _kb, "list_boards",
            side_effect=lambda *a, **kw: past_gate.append(True) or [],
        ):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                with patch("asyncio.to_thread", side_effect=fake_to_thread):
                    asyncio.run(runner._kanban_notifier_watcher())

    assert past_gate, "list_boards should be called when dispatch_in_gateway=true"
