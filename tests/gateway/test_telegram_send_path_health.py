"""TelegramAdapter send-path health gating after reconnect storms.

After sustained Bad Gateway / TimedOut reconnect cycles, the PTB httpx client
can enter a wedged state where ``bot.send_message()`` returns a valid Message
but nothing reaches the recipient.  ``_send_path_degraded`` short-circuits
``send()`` so cron's live-adapter branch falls through to standalone HTTP.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return adapter


@pytest.mark.asyncio
async def test_send_short_circuits_when_path_degraded():
    """Degraded adapter returns failure WITHOUT calling send_message,
    so cron's live-adapter branch falls through to standalone HTTP."""
    adapter = _make_adapter()
    adapter._send_path_degraded = True

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "send_path_degraded"
    assert result.retryable is True
    adapter._bot.send_message.assert_not_awaited()


class _FloodError(Exception):
    def __init__(self, seconds: float):
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")
        self.retry_after = seconds


@pytest.mark.asyncio
async def test_send_long_flood_fails_closed_without_inline_sleep(monkeypatch):
    """A 97-minute RetryAfter must not pin send() for the full penalty."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(5827.0))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "flood_control:5827.0"
    assert result.error_kind == "rate_limited"
    assert result.retry_after == 5827.0
    assert result.retryable is False
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_moderate_flood_defers_to_bounded_caller_retry(monkeypatch):
    """A normal Telegram cooldown is retryable without sleeping in send()."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(35.0))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == "flood_control:35.0"
    assert result.error_kind == "rate_limited"
    assert result.retry_after == 35.0
    assert result.retryable is True
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_bounded_delivery_retry_honors_moderate_telegram_cooldown(monkeypatch):
    """The real send wrapper delivers once Telegram's normal cooldown ends."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    delivered = MagicMock(message_id=77)
    adapter._bot.send_message = AsyncMock(side_effect=[_FloodError(35.0), delivered])
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", sleep)
    monkeypatch.setattr("gateway.platforms.base.random.uniform", lambda *_args: 0.0)

    result = await adapter._send_with_retry("123", "hello")

    assert result.success is True
    assert result.message_id == "77"
    assert adapter._bot.send_message.await_count == 2
    sleep.assert_awaited_once_with(35.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [2.0, 5.0])
async def test_send_short_flood_still_retries_inline(monkeypatch, wait):
    """Waits of a few seconds keep the existing inline retry."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    ok = MagicMock(message_id=7)
    adapter._bot.send_message = AsyncMock(side_effect=[_FloodError(wait), ok])
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is True
    assert result.message_id == "7"
    sleep.assert_awaited_once_with(wait)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wait", "retryable"),
    [(5.1, True), (60.0, True), (60.1, False)],
)
async def test_send_caller_retry_boundaries_fail_closed_without_inline_sleep(
    monkeypatch, wait, retryable
):
    """Values around both boundaries keep the inline/caller split exact."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(side_effect=_FloodError(wait))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("123", "hello")

    assert result.success is False
    assert result.error == f"flood_control:{wait}"
    assert result.error_kind == "rate_limited"
    assert result.retry_after == wait
    assert result.retryable is retryable
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_budget_honors_exact_60_and_clips_jitter(monkeypatch):
    """The exact cap gets one full caller retry without exceeding its budget."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    delivered = MagicMock(message_id=78)
    adapter._bot.send_message = AsyncMock(side_effect=[_FloodError(60.0), delivered])
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", sleep)
    monkeypatch.setattr("gateway.platforms.base.random.uniform", lambda *_args: 0.7)

    result = await adapter._send_with_retry("123", "hello")

    assert result.success is True
    assert result.message_id == "78"
    assert adapter._bot.send_message.await_count == 2
    sleep.assert_awaited_once_with(60.0)


@pytest.mark.asyncio
async def test_retry_after_budget_refuses_a_second_full_penalty(monkeypatch):
    """Consecutive moderate penalties cannot stack beyond one 60s budget."""
    adapter = _make_adapter()
    adapter._rich_send_disabled = True
    adapter._bot.send_message = AsyncMock(
        side_effect=[_FloodError(35.0), _FloodError(35.0)]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", sleep)
    monkeypatch.setattr("gateway.platforms.base.random.uniform", lambda *_args: 0.0)

    result = await adapter._send_with_retry("123", "hello")

    assert result.success is False
    assert result.error == "flood_control:35.0"
    assert result.retryable is True
    assert adapter._bot.send_message.await_count == 2
    sleep.assert_awaited_once_with(35.0)
