"""Regression tests for gateway reply-to context preparation."""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_reply_to_text_context_not_truncated_at_500_chars(monkeypatch):
    """Fix commit 30b891783 raised reply_to_text slicing from 500 to 8000 chars."""
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan-1",
        chat_type="channel",
        user_id="user-1",
        scope_id="guild-1",
    )
    replied_to_post = "long-post-start:" + ("x" * 1190) + ":long-post-end"
    event = MessageEvent(
        text="please summarize this",
        message_type=MessageType.TEXT,
        source=source,
        reply_to_message_id="orig-1",
        reply_to_text=replied_to_post,
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(group_sessions_per_user=True, thread_sessions_per_user=False)
    runner.adapters = {}
    monkeypatch.setattr(
        runner,
        "_session_key_for_source",
        lambda source: "agent:main:discord:channel:chan-1:user-1",
    )
    monkeypatch.setattr(runner, "_consume_pending_native_image_paths", lambda session_key: [])

    prepared = await GatewayRunner._prepare_inbound_message_text(
        runner,
        event=event,
        source=source,
        history=[],
    )

    assert prepared is not None
    reply_context, user_text = prepared.split("\n\n", 1)
    assert user_text == "please summarize this"
    assert replied_to_post in reply_context
    assert ":long-post-end" in reply_context
    assert len(reply_context) > 500
