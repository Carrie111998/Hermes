"""Tests for Telegram external-reply (cross-chat quote) handling.

Telegram delivers two distinct reply shapes:

* ``message.reply_to_message`` — a reply inside the same chat. The adapter
  already extracts its text via the quote → full-text → rich-echo chain.
* ``message.external_reply`` — a reply/quote pointing at a message in a
  *different* chat or channel (``ExternalReplyInfo``). No
  ``reply_to_message`` is populated in that case, so without explicit
  support the adapter previously dropped the reference entirely: the agent
  received a bare "what does this mean?" with zero context about what was
  being asked about.

These tests pin the external-reply contract: extract the quoted text (or a
caption for media origins), attribute it to the originating chat, and feed
it through the normal ``reply_to_*`` pipeline so ``_prepare_inbound_message_text``
injects the standard disambiguation prefix.
"""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    return TelegramAdapter(PlatformConfig(enabled=True, token="***", extra={}))


def _make_message(
    text="what does this mean?",
    external_reply=None,
    quote=None,
):
    chat = SimpleNamespace(id=111, type="private", title=None, full_name="Alice")
    user = SimpleNamespace(id=42, full_name="Alice")
    return SimpleNamespace(
        chat=chat,
        from_user=user,
        text=text,
        message_thread_id=None,
        message_id=1001,
        reply_to_message=None,
        external_reply=external_reply,
        quote=quote,
        date=None,
        forum_topic_created=None,
    )


def _external_reply(text=None, caption=None, origin_chat_title="News Channel", **origin_extra):
    origin = SimpleNamespace(chat=SimpleNamespace(title=origin_chat_title), **origin_extra)
    return SimpleNamespace(
        origin=origin,
        message_id=None,
        text=text,
        caption=caption,
    )


def test_external_reply_text_used_as_reply_to_text():
    """Quoted text from an external message becomes reply_to_text, prefixed
    with the origin chat title so the agent knows the context is external."""
    adapter = _make_adapter()
    msg = _make_message(
        external_reply=_external_reply(text="Breaking: new model released today"),
    )

    from gateway.platforms.base import MessageType

    event = adapter._build_message_event(msg, MessageType.TEXT)

    assert event.reply_to_text == (
        "News Channel: Breaking: new model released today"
    )
    assert event.reply_from_external is True
    # No real in-chat message id exists for an external reference.
    assert event.reply_to_message_id is None


def test_external_reply_caption_used_when_no_text():
    """Media-originating external replies expose their caption instead."""
    adapter = _make_adapter()
    msg = _make_message(
        external_reply=_external_reply(caption="Look at this chart"),
    )

    from gateway.platforms.base import MessageType

    event = adapter._build_message_event(msg, MessageType.TEXT)

    assert event.reply_to_text == "News Channel: Look at this chart"
    assert event.reply_from_external is True


def test_external_reply_quote_preferred_over_full_text():
    """A manual selection inside the external message wins over full text."""
    adapter = _make_adapter()
    msg = _make_message(
        external_reply=_external_reply(text="Full article body ..."),
        quote=SimpleNamespace(text="the one key sentence"),
    )

    from gateway.platforms.base import MessageType

    event = adapter._build_message_event(msg, MessageType.TEXT)

    assert event.reply_to_text == "News Channel: the one key sentence"
    assert event.reply_from_external is True


def test_external_reply_without_text_yields_none():
    """No text/caption/quote → no fabricated context."""
    adapter = _make_adapter()
    msg = _make_message(external_reply=_external_reply())

    from gateway.platforms.base import MessageType

    event = adapter._build_message_event(msg, MessageType.TEXT)

    assert event.reply_to_text is None
    assert event.reply_from_external is False


@pytest.mark.asyncio
async def test_external_reply_injected_with_source_attribution():
    """End-to-end: cross-chat quote gets the standard reply prefix, and when
    the origin is a different chat/channel the pointer says where it came
    from so the agent knows the context is external."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )
    event = MessageEvent(
        text="这是什么意思",
        source=source,
        reply_to_message_id=None,
        reply_to_text="Some quoted content from another channel",
        reply_from_external=True,
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[{"role": "user", "content": "unrelated"}],
    )

    assert result is not None
    assert '[Replying to: "Some quoted content from another channel"]' in result
    assert result.endswith("这是什么意思")
