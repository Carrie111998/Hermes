"""Regression tests for Teams placeholder edit-based streaming UX."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.teams.adapter import TeamsAdapter


@pytest.mark.asyncio
async def test_teams_inbound_message_sends_placeholder_and_carries_metadata():
    adapter = TeamsAdapter(PlatformConfig(extra={}))
    adapter._app = SimpleNamespace(id="bot-id")
    adapter._streaming_placeholder_enabled = lambda: True
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="placeholder-1"),
    )
    captured_events = []

    async def _capture(event):
        captured_events.append(event)

    adapter.handle_message = _capture

    activity = SimpleNamespace(
        id="incoming-1",
        text="<at>本部AI エルメス</at> 今日の天気は？",
        from_=SimpleNamespace(id="user-id", aad_object_id="aad-id", name="Yuta"),
        conversation=SimpleNamespace(
            id="conversation-1",
            name="honbu hermes（TEST）",
            conversation_type="channel",
            tenant_id="tenant-1",
        ),
        attachments=[],
    )
    ctx = SimpleNamespace(activity=activity, conversation_ref=SimpleNamespace())

    await adapter._on_message(ctx)

    adapter.send.assert_awaited_once_with(
        "conversation-1",
        "🤔 考え中...",
        reply_to="incoming-1",
    )
    assert captured_events
    assert captured_events[0].text == "今日の天気は？"
    assert captured_events[0].metadata["_stream_message_id"] == "placeholder-1"


@pytest.mark.asyncio
async def test_stream_consumer_edits_existing_placeholder_message():
    adapter = SimpleNamespace()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.supports_draft_streaming = lambda chat_type=None, metadata=None: False
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="unexpected-new-message"),
    )
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=True, message_id="placeholder-1"),
    )

    cfg = StreamConsumerConfig(
        transport="edit",
        edit_interval=0.01,
        buffer_threshold=1,
        cursor=" ▉",
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        cfg,
        metadata={"_stream_message_id": "placeholder-1"},
    )

    consumer.on_delta("hello")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    consumer.finish()
    await task

    adapter.send.assert_not_called()
    assert adapter.edit_message.await_args_list
    assert adapter.edit_message.await_args_list[0].kwargs["message_id"] == "placeholder-1"
    assert adapter.edit_message.await_args_list[-1].kwargs["finalize"] is True
    assert consumer.final_response_sent is True


@pytest.mark.asyncio
async def test_teams_typing_status_edits_placeholder_message():
    adapter = TeamsAdapter(PlatformConfig(extra={}))
    adapter._app = SimpleNamespace(send=AsyncMock())
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=True, message_id="placeholder-1"),
    )
    adapter.set_status_text("chat-1", "Web で検索中...")

    await adapter.send_typing(
        "chat-1",
        metadata={"_stream_message_id": "placeholder-1"},
    )

    adapter.edit_message.assert_awaited_once()
    call = adapter.edit_message.await_args
    assert call.args[:3] == (
        "chat-1",
        "placeholder-1",
        "🤔 Web で検索中...",
    )
    adapter._app.send.assert_not_called()
