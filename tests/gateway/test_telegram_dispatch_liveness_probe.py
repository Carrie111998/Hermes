"""TelegramAdapter dispatch-liveness detection: fetched but never dispatched.

The pending_update_count probe (#42909) and the stopped-updater probe (#55769)
both verify the FETCH half of the pipeline. Neither can see the inverse
failure: the updater RUNNING and FETCHING — PTB confirms updates to Telegram
on fetch, so ``pending_update_count`` stays 0 — while the Application's
update-processing task is dead, so fetched updates pile up in
``app.update_queue`` and never reach handlers. get_me() is healthy, fetch
progress advances, ``updater.running`` is True, the Bot API queue is empty:
every existing signal is green and the gateway is silently deaf.

``_probe_pending_updates`` therefore also compares ``update_queue.qsize()``
against a dispatch stamp written by a group -100 TypeHandler on every update
the Application dispatches. A non-empty queue with a frozen stamp across two
consecutive probes escalates to the retryable-fatal rebuild — deliberately
NOT the reconnect ladder, which restarts only the updater and would leave the
dead processing task exactly where it was.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(*, qsize: int, pending: int = 0) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._webhook_mode = False
    adapter._app = MagicMock()
    adapter._app.updater.running = True
    adapter._app.update_queue.qsize.return_value = qsize
    bot = MagicMock()
    bot.get_webhook_info = AsyncMock(
        return_value=MagicMock(pending_update_count=pending)
    )
    adapter._app.bot = bot
    adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_first_stalled_probe_only_counts():
    """Queue non-empty + frozen stamp: probe 1 increments, nothing escalates."""
    adapter = _make_adapter(qsize=3)
    adapter._last_dispatch_monotonic = 100.0
    adapter._dispatch_stamp_at_last_probe = 100.0
    fatal = AsyncMock()
    with patch.object(adapter, "_handoff_polling_fatal_error", new=fatal):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    assert adapter._polling_dispatch_stuck_count == 1
    fatal.assert_not_called()


@pytest.mark.asyncio
async def test_two_stalled_probes_force_retryable_fatal_rebuild():
    """The wedge state: two consecutive stuck probes rebuild the adapter.

    Escalation is the retryable-fatal handoff, not the reconnect ladder — an
    updater-only restart cannot revive a dead update-processing task.
    """
    adapter = _make_adapter(qsize=3)
    adapter._last_dispatch_monotonic = 100.0
    adapter._dispatch_stamp_at_last_probe = 100.0
    fatal = AsyncMock()
    set_fatal = MagicMock()
    ladder = AsyncMock()
    with patch.object(adapter, "_handoff_polling_fatal_error", new=fatal), \
         patch.object(adapter, "_set_fatal_error", new=set_fatal), \
         patch.object(adapter, "_handle_polling_network_error", new=ladder):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    fatal.assert_awaited_once()
    set_fatal.assert_called_once()
    assert set_fatal.call_args[0][0] == "telegram_dispatch_stalled"
    assert set_fatal.call_args[1].get("retryable") is True
    ladder.assert_not_called()
    assert adapter._polling_dispatch_stuck_count == 0


@pytest.mark.asyncio
async def test_busy_but_draining_queue_never_trips():
    """Stamp advanced between probes: high-throughput traffic is healthy."""
    adapter = _make_adapter(qsize=5)
    adapter._last_dispatch_monotonic = 100.0
    adapter._dispatch_stamp_at_last_probe = 90.0  # progressed since last probe
    fatal = AsyncMock()
    with patch.object(adapter, "_handoff_polling_fatal_error", new=fatal):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    assert adapter._polling_dispatch_stuck_count == 0
    fatal.assert_not_called()
    # And the stamp baseline moved forward for the next comparison.
    assert adapter._dispatch_stamp_at_last_probe == 100.0


@pytest.mark.asyncio
async def test_idle_queue_resets_the_counter():
    """qsize 0 is the resting state of every healthy idle bot."""
    adapter = _make_adapter(qsize=0)
    adapter._polling_dispatch_stuck_count = 1
    fatal = AsyncMock()
    with patch.object(adapter, "_handoff_polling_fatal_error", new=fatal):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    assert adapter._polling_dispatch_stuck_count == 0
    fatal.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_stamp_callback_advances_the_clock():
    """The group -100 observer stamps monotonic time on every dispatch."""
    adapter = _make_adapter(qsize=0)
    assert adapter._last_dispatch_monotonic == 0.0
    await adapter._note_dispatch_progress(MagicMock(), MagicMock())
    assert adapter._last_dispatch_monotonic > 0.0


@pytest.mark.asyncio
async def test_pending_probe_still_runs_after_healthy_dispatch_check():
    """The new block must not short-circuit the existing #42909 probe."""
    adapter = _make_adapter(qsize=0, pending=9)
    with patch.object(adapter, "_handle_polling_network_error", new=AsyncMock()):
        await adapter._probe_pending_updates(adapter._app.bot, 5)
    adapter._app.bot.get_webhook_info.assert_called_once()
    assert adapter._polling_pending_stuck_count == 1
