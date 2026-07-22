"""Telegram network-error recovery must NOT bounce the whole gateway.

Root-caused 2026-07-16 (NordVPN/DNS flap → ``getaddrinfo failed``): the
Telegram adapter's polling reconnect ladder gives up after
``MAX_NETWORK_RETRIES`` and raises a *retryable* fatal
(``telegram_network_error``).  The concern was that this bounced the entire
multi-platform gateway process, turning a ~10-minute network blip into a
restart storm that also killed WhatsApp / api_server / cron / the event bus.

These tests pin the correct behavior: a retryable Telegram network fatal is
routed through ``GatewayRunner._handle_adapter_fatal_error`` into the
``_failed_platforms`` reconnect-watcher queue.  The gateway process stays
alive (no ``stop()``, no ``exit_with_failure``) and every *other* connected
platform is left untouched, so only Telegram re-initializes — at the
watcher's backoff cadence — while the network recovers.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner
from plugins.platforms.telegram.adapter import TelegramAdapter

# MAX_NETWORK_RETRIES is a local constant inside
# ``_handle_polling_network_error``; mirror it here so the test drives the
# adapter to exactly the fatal-triggering attempt without importing internals.
_MAX_NETWORK_RETRIES = 10


class _ConnectedAdapter(BasePlatformAdapter):
    """A minimal, healthy adapter standing in for a second live platform."""

    def __init__(self, platform: Platform):
        super().__init__(PlatformConfig(enabled=True, token="token"), platform)
        self._mark_connected()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_runner(tmp_path) -> GatewayRunner:
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="tg-token"),
            Platform.WHATSAPP: PlatformConfig(enabled=True, token="wa-token"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    return GatewayRunner(config)


@pytest.mark.asyncio
async def test_telegram_10x_network_failure_does_not_restart_gateway(tmp_path):
    """A running Telegram adapter that exhausts its network-retry ladder while
    WhatsApp stays connected must NOT restart the gateway — it queues Telegram
    for background reconnection and leaves WhatsApp running.
    """
    runner = _make_runner(tmp_path)

    telegram = TelegramAdapter(PlatformConfig(enabled=True, token="tg-token"))
    telegram.set_fatal_error_handler(runner._handle_adapter_fatal_error)
    # Keep the reproduction focused on the runner's routing decision rather
    # than PTB teardown internals.
    telegram.disconnect = AsyncMock(wraps=telegram.disconnect)

    whatsapp = _ConnectedAdapter(Platform.WHATSAPP)

    runner.adapters = {Platform.TELEGRAM: telegram, Platform.WHATSAPP: whatsapp}
    runner.delivery_router.adapters = runner.adapters
    runner.stop = AsyncMock()

    # Drive the adapter to the attempt *past* the retry cap so the very next
    # network error trips the retryable fatal branch.
    telegram._polling_network_error_count = _MAX_NETWORK_RETRIES

    await telegram._handle_polling_network_error(
        OSError("getaddrinfo failed [Errno 11001]")
    )

    # Gateway process must stay alive.
    runner.stop.assert_not_awaited()
    assert runner.should_exit_with_failure is False
    assert runner.should_exit_cleanly is False

    # Telegram is torn down and queued for the reconnect watcher…
    telegram.disconnect.assert_awaited()
    assert Platform.TELEGRAM in runner._failed_platforms
    assert Platform.TELEGRAM not in runner.adapters

    # …while WhatsApp is completely untouched.
    assert runner.adapters.get(Platform.WHATSAPP) is whatsapp
    assert Platform.WHATSAPP not in runner._failed_platforms
    assert whatsapp.has_fatal_error is False


@pytest.mark.asyncio
async def test_telegram_network_fatal_is_retryable_with_expected_code(tmp_path):
    """The adapter contract the routing depends on: after the retry ladder is
    exhausted the fatal is *retryable* and coded ``telegram_network_error`` —
    the property that keeps it in the reconnect queue rather than dropping it
    (non-retryable) or restarting the process.
    """
    telegram = TelegramAdapter(PlatformConfig(enabled=True, token="tg-token"))
    captured = {}

    async def _handler(adapter):
        captured["code"] = adapter.fatal_error_code
        captured["retryable"] = adapter.fatal_error_retryable
        captured["message"] = adapter.fatal_error_message

    telegram.set_fatal_error_handler(_handler)
    telegram._polling_network_error_count = _MAX_NETWORK_RETRIES

    await telegram._handle_polling_network_error(
        OSError("getaddrinfo failed [Errno 11001]")
    )

    assert telegram.has_fatal_error is True
    assert captured["code"] == "telegram_network_error"
    assert captured["retryable"] is True
    # The message is surfaced to the operator (runtime status / notifications).
    # It must not falsely promise a gateway restart — recovery is adapter-only
    # via the reconnect watcher, so the old "Restarting gateway." wording lies
    # about what happens and misleads exactly during this kind of incident.
    message = (captured["message"] or "").lower()
    assert "restarting gateway" not in message
    assert "restarting the gateway" not in message


@pytest.mark.asyncio
async def test_telegram_sole_platform_network_fatal_keeps_gateway_alive(tmp_path):
    """Even when Telegram is the *only* messaging platform, exhausting the
    network-retry ladder must keep the gateway alive (cron + event bus stay
    up) and queue Telegram for reconnection — not exit-with-failure for a
    supervisor restart.
    """
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tg-token")},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    telegram = TelegramAdapter(PlatformConfig(enabled=True, token="tg-token"))
    telegram.set_fatal_error_handler(runner._handle_adapter_fatal_error)
    telegram.disconnect = AsyncMock(wraps=telegram.disconnect)

    runner.adapters = {Platform.TELEGRAM: telegram}
    runner.delivery_router.adapters = runner.adapters
    runner.stop = AsyncMock()

    telegram._polling_network_error_count = _MAX_NETWORK_RETRIES

    await telegram._handle_polling_network_error(
        OSError("getaddrinfo failed [Errno 11001]")
    )

    runner.stop.assert_not_awaited()
    assert runner.should_exit_with_failure is False
    assert Platform.TELEGRAM in runner._failed_platforms
