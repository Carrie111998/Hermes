from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType, Platform, SessionSource
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter


class _Response:
    status = 200

    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self._messages


@pytest.mark.asyncio
async def test_each_native_quote_is_claimed_before_whatsapp_text_batching():
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._bridge_port = 3000
    adapter._running = True
    adapter._http_session = SimpleNamespace(
        get=MagicMock(
            return_value=_Response(
                [
                    {"messageId": "m1", "quotedMessageId": "q1"},
                    {"messageId": "m2", "quotedMessageId": "q2"},
                ]
            )
        )
    )
    adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
    adapter._send_read_receipt = AsyncMock()
    adapter._enqueue_text_event = MagicMock()
    adapter.handle_message = AsyncMock()
    seen = []

    async def build(data):
        return MessageEvent(
            text=data["messageId"],
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.WHATSAPP,
                chat_id="codex@g.us",
                chat_type="group",
                user_id="15551234567",
            ),
            message_id=data["messageId"],
            reply_to_message_id=data["quotedMessageId"],
        )

    async def claim(event):
        seen.append((event.message_id, event.reply_to_message_id))
        if len(seen) == 2:
            adapter._running = False
        return True

    adapter._build_message_event = build
    adapter.dispatch_exclusive_inbound = claim

    await adapter._poll_messages()

    assert seen == [("m1", "q1"), ("m2", "q2")]
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_type",
    [
        "pollUpdateMessage",
        "pollCreationMessage",
        "pollCreationMessageV2",
        "pollCreationMessageV3",
        "reactionMessage",
    ],
)
async def test_native_interactions_bypass_exclusive_claim(native_type):
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter._bridge_port = 3000
    adapter._running = True
    adapter._http_session = SimpleNamespace(
        get=MagicMock(return_value=_Response([{"messageId": "poll-update"}]))
    )
    adapter._check_managed_bridge_exit = AsyncMock(return_value=None)
    adapter._send_read_receipt = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter.dispatch_exclusive_inbound = AsyncMock(return_value=True)

    async def build(_data):
        adapter._running = False
        return MessageEvent(
            text="Option A",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=Platform.WHATSAPP,
                chat_id="codex@g.us",
                chat_type="group",
                user_id="15551234567",
            ),
            message_id="poll-update",
            metadata={"whatsapp_native_type": native_type},
        )

    adapter._build_message_event = build
    adapter._enqueue_text_event = MagicMock()

    await adapter._poll_messages()

    adapter.dispatch_exclusive_inbound.assert_not_awaited()
    adapter._enqueue_text_event.assert_called_once()
