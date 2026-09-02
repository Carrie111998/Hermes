"""Regression tests for stream consumer thread/topic routing fix.

Verifies that GatewayStreamConsumer correctly passes reply_to on the first
message send, ensuring messages land in the correct topic/thread instead of
the main group chat.

Covers: #6969, #9916, #7355
"""
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from gateway.stream_consumer import (
    GatewayStreamConsumer,
)


def _make_adapter(send_result=None, edit_result=None, max_length=4096):
    adapter = MagicMock()
    adapter.send = AsyncMock(
        return_value=send_result or SimpleNamespace(success=True, message_id="msg_1")
    )
    adapter.edit_message = AsyncMock(
        return_value=edit_result or SimpleNamespace(success=True)
    )
    adapter.MAX_MESSAGE_LENGTH = max_length
    return adapter


class TestInitialReplyToId:
    """Verify initial_reply_to_id is passed as reply_to on first send."""

    @pytest.mark.asyncio
    async def test_first_send_uses_initial_reply_to_id(self):
        """When initial_reply_to_id is set, first adapter.send() should
        include reply_to=initial_reply_to_id."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_123",
            metadata={"thread_id": "omt_topic123"},
            initial_reply_to_id="om_user_msg_456",
        )
        await consumer._send_or_edit("Hello world")

        adapter.send.assert_called_once()
        call_kwargs = adapter.send.call_args[1]
        assert call_kwargs["reply_to"] == "om_user_msg_456", (
            "First send should pass initial_reply_to_id as reply_to"
        )
        assert call_kwargs["chat_id"] == "chat_123"


    @pytest.mark.asyncio
    async def test_subsequent_edits_ignore_initial_reply_to_id(self):
        """After first send, edits should use message_id, not initial_reply_to_id."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_123",
            metadata={"thread_id": "omt_topic123"},
            initial_reply_to_id="om_user_msg_456",
        )

        # First send
        await consumer._send_or_edit("Hello world")
        assert adapter.send.call_count == 1

        # Second call should edit, not send
        await consumer._send_or_edit("Hello world updated")
        assert adapter.send.call_count == 1, "Should edit, not send again"
        adapter.edit_message.assert_called_once()
        edit_kwargs = adapter.edit_message.call_args[1]
        assert edit_kwargs["message_id"] == "msg_1"
        assert edit_kwargs["chat_id"] == "chat_123"


class TestOverflowFirstMessage:
    """Verify thread routing is preserved when the first message overflows."""

    @pytest.mark.asyncio
    async def test_overflow_first_send_uses_initial_reply_to_id(self):
        """When first message exceeds platform limit and is split into chunks,
        each chunk should be threaded to initial_reply_to_id, not None."""
        adapter = _make_adapter(max_length=10)
        adapter.truncate_message = MagicMock(
            return_value=["chunk_1", "chunk_2"]
        )
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_123",
            metadata={"thread_id": "omt_topic123"},
            initial_reply_to_id="om_user_msg_789",
        )

        # Inject oversized accumulated text to trigger overflow path
        consumer._accumulated = "A" * 100
        consumer._current_edit_interval = 999
        await consumer._send_new_chunk("chunk_1", consumer._message_id or consumer._initial_reply_to_id)

        adapter.send.assert_called_once()
        call_kwargs = adapter.send.call_args[1]
        assert call_kwargs["reply_to"] == "om_user_msg_789", (
            "Overflow first chunk should use initial_reply_to_id"
        )


class TestFeishuFallbackThreadRouting:
    """Verify FeishuAdapter._send_raw_message routes to topic on fallback.

    The Feishu create-message API does NOT accept ``receive_id_type=thread_id``
    — it rejects the request with 99992402 ``field validation failed`` and
    reports ``options: [open_id,user_id,union_id,email,chat_id]``. So a
    reply→create fallback for a topic message must anchor on the last message
    in the thread and use the reply API with ``reply_in_thread=true``.
    """

    @staticmethod
    def _make_raw_adapter(mock_client):
        """Build a MagicMock adapter wired to the real _send_raw_message deps."""
        from plugins.platforms.feishu.adapter import FeishuAdapter

        adapter = MagicMock(spec=FeishuAdapter)
        adapter._client = mock_client
        adapter._build_create_message_body = FeishuAdapter._build_create_message_body
        adapter._build_create_message_request = FeishuAdapter._build_create_message_request
        adapter._build_reply_message_body = FeishuAdapter._build_reply_message_body
        adapter._build_reply_message_request = FeishuAdapter._build_reply_message_request
        # _send_raw_message routes blocking SDK calls through _run_blocking
        # (adapter-owned executor). On a MagicMock(spec=...) that method is
        # auto-mocked and would swallow the real call, so wire a passthrough.
        async def _run_blocking_passthrough(func, *args):
            return func(*args)
        adapter._run_blocking = _run_blocking_passthrough
        return adapter

    @pytest.mark.asyncio
    async def test_thread_fallback_replies_in_thread_instead_of_create(self):
        """When reply_to=None and metadata has thread_id, the adapter must
        anchor on the thread's last message and call message.reply with
        reply_in_thread=True — never message.create with thread_id."""
        import json

        mock_client = MagicMock()
        mock_reply_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="new_msg_1"),
        )
        mock_client.im.v1.message.reply = MagicMock(return_value=mock_reply_response)
        mock_client.im.v1.message.create = MagicMock()

        adapter = self._make_raw_adapter(mock_client)

        async def _fetch_anchor(thread_id):
            assert thread_id == "omt_topic_abc"
            return "om_anchor_msg"
        adapter._fetch_last_message_in_thread = _fetch_anchor

        from plugins.platforms.feishu.adapter import FeishuAdapter
        await FeishuAdapter._send_raw_message(
            adapter,
            chat_id="oc_main_chat",
            msg_type="text",
            payload=json.dumps({"text": "hello"}),
            reply_to=None,
            metadata={"thread_id": "omt_topic_abc"},
        )

        # reply API is used; create() must NOT be called with thread_id, since
        # the API rejects receive_id_type=thread_id with 99992402.
        mock_client.im.v1.message.reply.assert_called_once()
        mock_client.im.v1.message.create.assert_not_called()

        # The reply must be anchored on the thread's last message...
        call_args = mock_client.im.v1.message.reply.call_args[0][0]
        message_id = getattr(call_args, "message_id", None)
        assert message_id == "om_anchor_msg", (
            f"Expected reply anchored on 'om_anchor_msg', got '{message_id}'"
        )
        # ...and carry reply_in_thread=True so it lands in the topic.
        body = getattr(call_args, "body", None) or getattr(call_args, "request_body", None)
        assert body is not None, "request has neither .body nor .request_body"
        reply_in_thread = getattr(body, "reply_in_thread", None)
        if reply_in_thread is None and isinstance(body, str):
            import json as _json
            reply_in_thread = _json.loads(body).get("reply_in_thread")
        assert reply_in_thread is True, (
            f"Expected reply_in_thread=True, got {reply_in_thread!r}"
        )

    @pytest.mark.asyncio
    async def test_thread_fallback_without_anchor_delivers_to_main_chat(self):
        """If no anchor message can be found (empty thread / missing read
        scope), the message must still be delivered via a chat_id create
        rather than silently lost."""
        import json

        mock_client = MagicMock()
        mock_create_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="new_msg_2"),
        )
        mock_client.im.v1.message.create = MagicMock(return_value=mock_create_response)
        mock_client.im.v1.message.reply = MagicMock()

        adapter = self._make_raw_adapter(mock_client)

        async def _no_anchor(thread_id):
            return None
        adapter._fetch_last_message_in_thread = _no_anchor

        from plugins.platforms.feishu.adapter import FeishuAdapter
        await FeishuAdapter._send_raw_message(
            adapter,
            chat_id="oc_main_chat",
            msg_type="text",
            payload=json.dumps({"text": "hello"}),
            reply_to=None,
            metadata={"thread_id": "omt_topic_abc"},
        )

        mock_client.im.v1.message.reply.assert_not_called()
        mock_client.im.v1.message.create.assert_called_once()

        # Falls back to a chat_id create — an API-accepted receive_id_type.
        call_args = mock_client.im.v1.message.create.call_args[0][0]
        assert getattr(call_args, "receive_id_type", None) == "chat_id"
        body = getattr(call_args, "body", None) or getattr(call_args, "request_body", None)
        receive_id = getattr(body, "receive_id", None)
        if receive_id is None and isinstance(body, str):
            import json as _json
            receive_id = _json.loads(body).get("receive_id")
        assert receive_id == "oc_main_chat"

