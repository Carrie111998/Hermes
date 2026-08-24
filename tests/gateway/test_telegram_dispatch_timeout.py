"""Regression tests for bounded Telegram pre-dispatch work (#93506)."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


def _event(text: str = "hello") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="42",
            chat_id="42",
            chat_type="dm",
        ),
    )


def _adapter(text: str) -> tuple[TelegramAdapter, SimpleNamespace]:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._inbound_preprocess_timeout_seconds = 0.01
    adapter._is_user_authorized_from_message = lambda _msg: True
    adapter._should_process_message = lambda _msg, **_kwargs: True
    adapter._build_message_event = lambda *_args, **_kwargs: _event(text)
    adapter._clean_bot_trigger_text = lambda value: value
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    message = SimpleNamespace(text=text)
    update = SimpleNamespace(update_id=1, message=message, effective_message=message)
    return adapter, update


async def _complete(*_args) -> None:
    return None


async def _hang(event: asyncio.Event, *_args) -> None:
    await event.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("hung_stage", ["forum", "media"])
async def test_text_dispatch_survives_hung_optional_preprocessing(
    hung_stage: str,
) -> None:
    """Optional Telegram lookups must not serialize-stall update dispatch."""
    adapter, update = _adapter("hello")
    never_finishes = asyncio.Event()
    adapter._ensure_forum_commands = (
        lambda *args: _hang(never_finishes, *args)
        if hung_stage == "forum"
        else _complete(*args)
    )
    adapter._cache_replied_media = (
        lambda *args: _hang(never_finishes, *args)
        if hung_stage == "media"
        else _complete(*args)
    )
    queued: list[MessageEvent] = []
    adapter._enqueue_text_event = queued.append

    await asyncio.wait_for(
        adapter._handle_text_message(update, SimpleNamespace()),
        timeout=0.25,
    )

    assert [event.text for event in queued] == ["hello"]


@pytest.mark.asyncio
async def test_command_dispatch_survives_hung_optional_preprocessing() -> None:
    adapter, update = _adapter("/status")
    never_finishes = asyncio.Event()
    adapter._ensure_forum_commands = _complete
    adapter._cache_replied_media = lambda *args: _hang(never_finishes, *args)
    dispatched: list[MessageEvent] = []

    async def handle_message(event: MessageEvent) -> None:
        dispatched.append(event)

    adapter.handle_message = handle_message
    await asyncio.wait_for(
        adapter._handle_command(update, SimpleNamespace()),
        timeout=0.25,
    )

    assert [event.text for event in dispatched] == ["/status"]
