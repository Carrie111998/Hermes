"""Telegram send() must wait for reconnect instead of dropping the final reply.

A network blip can disconnect Telegram while the agent is sending its final
response. send() used to return "Not connected" immediately (retryable=False),
so the delivery ledger held the answer until the next gateway boot — hours
later. QQBot already waits; these tests pin the same contract on Telegram,
including the reconnect-watcher case that *replaces* the adapter object.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    adapter.send_typing = AsyncMock()
    adapter._RECONNECT_WAIT_SECONDS = 0.6
    adapter._RECONNECT_POLL_INTERVAL = 0.05
    return adapter


def _connected_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return bot


@pytest.mark.asyncio
async def test_send_waits_and_succeeds_when_bot_returns():
    adapter = _make_adapter()
    adapter._bot = None

    async def restore_bot() -> None:
        await asyncio.sleep(0.12)
        adapter._bot = _connected_bot()

    asyncio.get_running_loop().create_task(restore_bot())
    result = await adapter.send("123", "hello")

    assert result.success is True
    assert result.message_id == "42"
    adapter._bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_delegates_to_replacement_adapter_installed_mid_wait():
    old = _make_adapter()
    old._bot = None

    live = _make_adapter()
    live._bot = _connected_bot()
    live._RECONNECT_WAIT_SECONDS = 0.01

    runner = MagicMock()
    runner.adapters = {}
    old.gateway_runner = runner

    async def install_replacement() -> None:
        await asyncio.sleep(0.12)
        runner.adapters[old.platform] = live

    asyncio.get_running_loop().create_task(install_replacement())
    result = await old.send("123", "hello")

    assert result.success is True
    assert result.message_id == "42"
    live._bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_delegates_immediately_when_replacement_already_live():
    old = _make_adapter()
    old._bot = None
    live = _make_adapter()
    live._bot = _connected_bot()
    runner = MagicMock()
    runner.adapters = {old.platform: live}
    old.gateway_runner = runner
    old._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait when replacement is already live")
    )

    result = await old.send("123", "hello")

    assert result.success is True
    live._bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_timeout_is_retryable_not_connected():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._RECONNECT_WAIT_SECONDS = 0.15
    adapter._RECONNECT_POLL_INTERVAL = 0.05

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_send_permanent_fatal_fails_immediately_without_wait():
    adapter = _make_adapter()
    adapter._bot = None
    adapter._set_fatal_error("telegram_auth_error", "invalid token", retryable=False)
    adapter._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait on permanent fatal")
    )

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_edit_message_delegates_to_replacement_when_already_live():
    """#98228: an in-flight progress edit on a retired adapter must resolve
    through the connected replacement instead of failing 'Not connected' and
    fragmenting later progress into fresh messages."""
    old = _make_adapter()
    old._bot = None
    live = _make_adapter()
    live._bot = _connected_bot()
    live.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="7"))
    runner = MagicMock()
    runner.adapters = {old.platform: live}
    old.gateway_runner = runner
    old._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait when replacement is already live")
    )

    result = await old.edit_message("123", "7", "updated")

    assert result.success is True
    live.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_message_delegates_to_replacement_when_already_live():
    """#98228: post-delivery cleanup on a retired adapter must delete through
    the connected replacement so temporary progress/heartbeat messages don't
    linger after a reconnect."""
    old = _make_adapter()
    old._bot = None
    live = _make_adapter()
    live._bot = _connected_bot()
    live.delete_message = AsyncMock(return_value=True)
    runner = MagicMock()
    runner.adapters = {old.platform: live}
    old.gateway_runner = runner
    old._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait when replacement is already live")
    )

    assert await old.delete_message("123", "7") is True
    live.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_message_permanent_fatal_fails_closed_without_delegation():
    """A permanently fatal (auth) failure must fail closed — never wait or
    delegate across an incompatible identity."""
    adapter = _make_adapter()
    adapter._bot = None
    adapter._set_fatal_error("telegram_auth_error", "invalid token", retryable=False)
    adapter._wait_for_reconnection = AsyncMock(
        side_effect=AssertionError("must not wait on permanent fatal")
    )

    result = await adapter.edit_message("123", "7", "updated")

    assert result.success is False
    assert result.error == "Not connected"
    assert result.retryable is False
