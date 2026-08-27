"""Regression tests for the two Slack native-streaming bugs found live
on 2026-08-27 (xai-hermes, thread 1787860083.556349):

1. ``send_native_task_card_progress`` sent BOTH ``chunks`` and
   ``markdown_text`` in the same chat.appendStream call; Slack rejects
   that combination with ``cannot_provide_both_markdown_text_and_chunks``,
   so every task-card update after the first failed and the gateway fell
   back to plain-text progress posts (which then landed as extra thread
   messages).

2. ``SlackAdapter`` did not declare ``draft_stream_is_message`` even
   though its ``send()`` intercepts turn-finals via
   ``_try_finalize_stream`` (stream-is-the-message semantics, same as
   the relay Slack adapter which DOES declare it). The stream consumer
   therefore applied Telegram-shaped draft semantics: real sends at
   tool-boundary segment breaks sealed the live stream mid-turn, and the
   remainder of the answer arrived as separate posts — duplicated
   content in the thread.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.slack.adapter import SlackAdapter


def _bare_adapter() -> SlackAdapter:
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.config = SimpleNamespace(extra={"native_task_cards": True})
    adapter._app = MagicMock()
    adapter._team_clients = {}
    adapter._native_task_card_streams = {}
    adapter._channel_team = {}
    adapter._bot_message_ts = set()
    return adapter


class TestNativeTaskCardAppendPayload:
    """Bug 1: chunks and markdown_text are mutually exclusive."""

    @pytest.mark.asyncio
    async def test_append_stream_never_mixes_chunks_and_markdown_text(self):
        adapter = _bare_adapter()
        client = AsyncMock()

        async def api_call(method, *, json):
            if method == "chat.startStream":
                return {"ts": "stream-1"}
            assert not ("chunks" in json and "markdown_text" in json), (
                "chat.appendStream must not carry both chunks and "
                "markdown_text (Slack error: "
                "cannot_provide_both_markdown_text_and_chunks)"
            )
            return {"ok": True}

        client.api_call.side_effect = api_call
        adapter._team_clients["T1"] = client
        metadata = {"thread_id": "thread-1", "slack_team_id": "T1"}
        tasks = [{"id": "call-1", "title": "terminal", "status": "in_progress"}]

        result = await adapter.send_native_task_card_progress(
            "C1", tasks, metadata=metadata, fallback_text="Hermes is working"
        )

        assert result.success, result.error
        append_calls = [
            call
            for call in client.api_call.await_args_list
            if call.args[0] == "chat.appendStream"
        ]
        assert append_calls, "expected at least one chat.appendStream call"
        for call in append_calls:
            payload = call.kwargs["json"]
            assert "chunks" in payload
            assert "markdown_text" not in payload


class TestStreamIsMessageDeclaration:
    """Bug 2: the native adapter must declare stream-is-the-message."""

    def test_class_declares_draft_stream_is_message(self):
        # The stream consumer resolves this on the CLASS (MagicMock safety),
        # so the declaration must be a class attribute, not instance state.
        assert SlackAdapter.draft_stream_is_message is True

    def test_consumer_probe_sees_stream_is_message(self):
        from gateway.stream_consumer import GatewayStreamConsumer

        consumer = GatewayStreamConsumer.__new__(GatewayStreamConsumer)
        consumer.adapter = _bare_adapter()
        consumer.chat_id = "C1"
        assert consumer._stream_is_message() is True
