"""Regression coverage for bounded Telegram flood-control retries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as telegram_adapter


class _RetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


def _adapter_with_send(side_effect):
    adapter = telegram_adapter.TelegramAdapter(
        PlatformConfig(enabled=True, token="fake-token", extra={})
    )
    adapter._rich_messages_enabled = False
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=side_effect)
    adapter._bot.send_chat_action = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_oversized_retry_after_fails_without_sleeping_or_retrying(monkeypatch):
    """A server penalty measured in hours must not hold the boot path open."""
    adapter = _adapter_with_send(_RetryAfter(5827))
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", sleep)

    result = await adapter.send("123", "gateway restarted")

    assert result.success is False
    assert result.error_kind == "rate_limited"
    adapter._bot.send_message.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_retry_after_remains_retryable(monkeypatch):
    """Normal short penalties still use the existing bounded retry path."""
    adapter = _adapter_with_send(
        [_RetryAfter(2), SimpleNamespace(message_id=42)]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_adapter.asyncio, "sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is True
    assert result.message_id == "42"
    assert adapter._bot.send_message.await_count == 2
    sleep.assert_awaited_once_with(2.0)
