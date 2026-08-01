"""Best-effort Telegram status delivery stays a one-request plain-text path."""

from __future__ import annotations

import sys
import types
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _install_fake_telegram(monkeypatch):
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = SimpleNamespace(ALL_TYPES=())
    fake_telegram.Bot = object
    fake_telegram.Message = object
    fake_telegram.InlineKeyboardButton = object
    fake_telegram.InlineKeyboardMarkup = object

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = type("NetworkError", (Exception,), {})
    fake_error.BadRequest = type("BadRequest", (Exception,), {})
    fake_error.TimedOut = type("TimedOut", (Exception,), {})
    fake_telegram.error = fake_error

    fake_constants = types.ModuleType("telegram.constants")
    fake_constants.ParseMode = SimpleNamespace(MARKDOWN_V2="MarkdownV2")
    fake_constants.ChatType = SimpleNamespace(
        GROUP="group", SUPERGROUP="supergroup", CHANNEL="channel", PRIVATE="private",
    )
    fake_telegram.constants = fake_constants

    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.Application = object
    fake_ext.CommandHandler = object
    fake_ext.CallbackQueryHandler = object
    fake_ext.MessageHandler = object
    fake_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    fake_ext.filters = object

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = object

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", fake_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)


@pytest.fixture
def adapter(monkeypatch):
    _install_fake_telegram(monkeypatch)
    from plugins.platforms.telegram.adapter import TelegramAdapter

    result = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    result._bot = MagicMock()
    result._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    result._bot.edit_message_text = AsyncMock()
    return result


@pytest.mark.asyncio
async def test_best_effort_status_sends_plain_text_once_without_formatting_or_fallback(adapter):
    result = await adapter.send_or_update_status(
        "123",
        "delegation:abc123",
        "Delegation abc123\nstatus: running",
        metadata={"thread_id": "42"},
        best_effort=True,
    )

    assert result.success is True
    assert result.message_id == "77"
    adapter._bot.send_message.assert_awaited_once_with(
        chat_id=123,
        text="Delegation abc123\nstatus: running",
        message_thread_id=42,
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_best_effort_status_preserves_private_dm_topic_reply_anchor(adapter):
    result = await adapter.send_or_update_status(
        "123",
        "delegation:abc123",
        "Delegation abc123\nstatus: running",
        metadata={
            "thread_id": "42",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": 462,
        },
        best_effort=True,
    )

    assert result.success is True
    adapter._bot.send_message.assert_awaited_once_with(
        chat_id=123,
        text="Delegation abc123\nstatus: running",
        reply_to_message_id=462,
        message_thread_id=42,
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_best_effort_status_fails_closed_when_private_dm_topic_anchor_is_missing(adapter):
    result = await adapter.send_or_update_status(
        "123",
        "delegation:abc123",
        "Delegation abc123\nstatus: running",
        metadata={
            "thread_id": "42",
            "telegram_dm_topic_reply_fallback": True,
        },
        best_effort=True,
    )

    assert result.success is False
    assert result.retryable is False
    assert "requires a reply anchor" in (result.error or "")
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_best_effort_status_edits_once_without_topic_kwargs_or_fallback_and_terminal_cleans_cache(adapter):
    adapter._status_message_ids[("123", "delegation:abc123")] = "77"

    result = await adapter.send_or_update_status(
        "123",
        "delegation:abc123",
        "Delegation abc123\nstatus: completed",
        metadata={"thread_id": "42"},
        best_effort=True,
        terminal=True,
    )

    assert result.success is True
    adapter._bot.edit_message_text.assert_awaited_once_with(
        chat_id=123,
        message_id=77,
        text="Delegation abc123\nstatus: completed",
    )
    adapter._bot.send_message.assert_not_awaited()
    assert ("123", "delegation:abc123") not in adapter._status_message_ids


@pytest.mark.asyncio
async def test_best_effort_retry_after_returns_once_without_sleep_or_fallback(adapter):
    class RetryAfterError(Exception):
        retry_after = timedelta(seconds=45)

    adapter._bot.send_message.side_effect = RetryAfterError("retry after 45")

    result = await adapter.send_or_update_status(
        "123",
        "delegation:abc123",
        "Delegation abc123\nstatus: running",
        best_effort=True,
    )

    assert result.success is False
    assert result.retry_after == 45.0
    adapter._bot.send_message.assert_awaited_once()
    adapter._bot.edit_message_text.assert_not_awaited()
    assert ("123", "delegation:abc123") not in adapter._status_message_ids
