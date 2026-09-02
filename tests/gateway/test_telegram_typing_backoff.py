"""Telegram typing indicator transient backoff tests."""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_typing_transient_failure_enters_cooldown(monkeypatch):
    adapter = _make_adapter()
    now = {"value": 1000.0}
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.get_running_loop", lambda: type("Loop", (), {"time": lambda self: now["value"]})())
    monkeypatch.setattr(adapter, "_telegram_typing_cooldown_seconds", 30.0, raising=False)

    async def fail_once(**kwargs):
        raise OSError("temporary telegram network failure")

    adapter._bot.send_chat_action = AsyncMock(side_effect=fail_once)

    await adapter.send_typing("123")
    await adapter.send_typing("123")

    assert adapter._bot.send_chat_action.await_count == 1
    assert adapter._telegram_typing_cooldown_until["123"] == pytest.approx(1030.0)

    now["value"] = 1031.0
    adapter._bot.send_chat_action = AsyncMock(return_value=None)
    await adapter.send_typing("123")

    assert adapter._bot.send_chat_action.await_count == 1
    assert "123" not in adapter._telegram_typing_cooldown_until


@pytest.mark.asyncio
async def test_typing_dm_topic_fallback_success_does_not_cool_down(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.get_running_loop", lambda: type("Loop", (), {"time": lambda self: 10.0})())

    calls = []

    async def send_chat_action(**kwargs):
        calls.append(kwargs)
        if "message_thread_id" in kwargs:
            raise RuntimeError("message thread not found")
        return None

    adapter._bot.send_chat_action = AsyncMock(side_effect=send_chat_action)

    await adapter.send_typing(
        "123",
        metadata={"thread_id": "99", "telegram_dm_topic_reply_fallback": True},
    )

    assert len(calls) == 2
    assert "123" not in adapter._telegram_typing_cooldown_until


@pytest.mark.asyncio
async def test_typing_bad_thread_failure_does_not_cool_down(monkeypatch):
    adapter = _make_adapter()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.get_running_loop", lambda: type("Loop", (), {"time": lambda self: 10.0})())

    async def bad_request(**kwargs):
        raise ValueError("message thread not found")

    adapter._bot.send_chat_action = AsyncMock(side_effect=bad_request)

    await adapter.send_typing("123", metadata={"thread_id": "99"})
    await adapter.send_typing("123", metadata={"thread_id": "99"})

    assert adapter._bot.send_chat_action.await_count == 2
    assert "123" not in adapter._telegram_typing_cooldown_until


@pytest.mark.asyncio
async def test_send_typing_noops_during_send_cooldown():
    """sendChatAction must not fire while the chat is in send cooldown.

    The per-chat send cooldown (#66722) gates send()/edit. Typing used to
    walk past it and keep hammering a flood-limited chat (#99643).
    """
    adapter = _make_adapter()
    adapter._bot.send_chat_action = AsyncMock(return_value=None)
    adapter._send_cooldown_until = {"123": time.monotonic() + 30.0}

    await adapter.send_typing("123")

    adapter._bot.send_chat_action.assert_not_called()

    # A different chat is not blocked.
    await adapter.send_typing("456")
    adapter._bot.send_chat_action.assert_called_once()

    # After the cooldown expires, typing resumes.
    adapter._send_cooldown_until["123"] = time.monotonic() - 0.1
    adapter._bot.send_chat_action.reset_mock()
    await adapter.send_typing("123")
    adapter._bot.send_chat_action.assert_called_once()
