"""Published platform state must follow the receive path after connect.

``_mark_connected()`` stamps ``platform_state: "connected"`` exactly once and
nothing rewrites it until the adapter disconnects or the recovery ladder
escalates to a fatal.  When Telegram polling dies in between — the ladder is
still retrying, or has itself wedged — ``gateway_state.json`` keeps reporting
``connected`` with ``error_code: null``, which is byte-for-byte what a healthy
adapter publishes.  One seat measured ~11h of that false ``connected`` (#101391).

``_send_path_degraded`` is already the adapter's own authoritative answer, and
the polling heartbeat already runs for the whole lifetime of the connection, so
these tests pin the heartbeat mirroring that flag into the published state.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return adapter


def _connected_adapter() -> TelegramAdapter:
    """An adapter whose connect() already published a healthy ``connected``."""
    adapter = _make_adapter()
    adapter._running = True
    adapter._send_path_degraded = False
    adapter._published_polling_degraded = False
    return adapter


def _single_heartbeat_tick(monkeypatch) -> None:
    """Let ``_polling_heartbeat_loop`` run exactly one tick, then unwind."""
    ticks = {"n": 0}

    async def _fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep", _fake_sleep
    )


@pytest.mark.asyncio
async def test_heartbeat_publishes_degraded_after_polling_dies(monkeypatch):
    """The reported bug: polling dies after a healthy connect and the published
    state stays ``connected`` for the whole recovery window."""
    adapter = _connected_adapter()
    # Every polling-death site sets this (network error, conflict, wedged
    # bootstrap, failed deleteWebhook). None of them publishes a state.
    adapter._send_path_degraded = True
    # No live PTB app -> the tick skips the get_me() probe and loops.
    adapter._app = None
    _single_heartbeat_tick(monkeypatch)

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        await adapter._polling_heartbeat_loop()

    write_status.assert_called_once()
    _, kwargs = write_status.call_args
    assert kwargs["platform_state"] == "retrying"
    assert kwargs["error_message"]


@pytest.mark.asyncio
async def test_heartbeat_is_silent_while_polling_stays_healthy(monkeypatch):
    """A steady-state adapter must not rewrite the status file every 90s."""
    adapter = _connected_adapter()
    adapter._app = None
    _single_heartbeat_tick(monkeypatch)

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        await adapter._polling_heartbeat_loop()

    write_status.assert_not_called()


def test_publish_republishes_connected_once_polling_recovers():
    """After the degraded state is published, a confirmed getUpdates round-trip
    must move the published state back rather than leaving it wedged."""
    adapter = _connected_adapter()
    adapter._send_path_degraded = True
    adapter._publish_polling_health()
    assert adapter._published_polling_degraded is True

    # _record_polling_progress clears the flag on a real round-trip.
    adapter._send_path_degraded = False

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        adapter._publish_polling_health()

    write_status.assert_called_once()
    _, kwargs = write_status.call_args
    assert kwargs["platform_state"] == "connected"
    assert kwargs["error_code"] is None
    assert kwargs["error_message"] is None


def test_publish_does_not_write_twice_for_the_same_health():
    """Only transitions are published; the ladder can retry for hours."""
    adapter = _connected_adapter()
    adapter._send_path_degraded = True

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        adapter._publish_polling_health()
        adapter._publish_polling_health()
        adapter._publish_polling_health()

    write_status.assert_called_once()


def test_publish_never_overwrites_a_fatal_verdict():
    """``fatal``/retryable-fatal is a stronger verdict than degraded polling."""
    adapter = _connected_adapter()
    adapter._send_path_degraded = True
    adapter._fatal_error_code = "telegram_network_error"
    adapter._fatal_error_message = "ladder exhausted"

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        adapter._publish_polling_health()

    write_status.assert_not_called()


def test_publish_never_overwrites_disconnected():
    """disconnect() clears ``_running`` before it raises the teardown fence, so
    a heartbeat mid-tick must not stamp health over ``disconnected``."""
    adapter = _connected_adapter()
    adapter._send_path_degraded = True
    adapter._running = False

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        adapter._publish_polling_health()

    write_status.assert_not_called()


def test_publish_is_inert_after_teardown_starts():
    adapter = _connected_adapter()
    adapter._send_path_degraded = True
    adapter._polling_teardown_started = True

    with patch.object(adapter, "_write_runtime_status_safe") as write_status:
        adapter._publish_polling_health()

    write_status.assert_not_called()
