"""Regression test: Telegram group messages must always include the bot's
@-mention handle in ``channel_prompt``, regardless of the
``observe_unmentioned_group_messages`` setting.

Before this fix, the bot-identity hint was injected only on the
``observe_unmentioned_group_messages`` code path. With that setting off (the
default), a group message addressed to the bot as ``@<bot> test`` would be
dispatched to the agent loop with no context about who the bot is. The model
would treat the leading ``@<bot>`` mention as an unknown third-party handle
and stay silent, because:

1. ``_clean_bot_trigger_text`` strips ``@<bot>`` from the message text, so the
   agent only sees the bare ``test`` with no indication it was addressed to
   the bot.
2. ``channel_prompt`` had no mention of the bot's @-username, so the agent
   had no way to recognize that the (now-stripped) prefix referred to itself.

After this fix, every Telegram group message carries a short identity block
in ``channel_prompt`` so the agent always knows its own @-mention handle.
"""

import asyncio
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType, SessionSource


def _make_adapter(bot_username=None, observe_unmentioned=False):
    """Build a TelegramAdapter stub."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {
        "allowed_chats": [],
        "group_allowed_chats": [],
        "allowed_topics": [],
        "mention_patterns": [],
        "observe_unmentioned_group_messages": observe_unmentioned,
    }
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(
        id=999, username=bot_username, get_me=AsyncMock()
    )
    adapter._bot_username_observed = bot_username
    adapter._bot_identity_checked_at = 0.0
    adapter._webhook_mode = False
    return adapter


def _make_event(chat_type="supergroup", text="hello"):
    """Build a real MessageEvent with the minimum attributes the adapter reads."""
    from gateway.platforms.base import MessageEvent

    raw = SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type=chat_type),
        from_user=SimpleNamespace(id=42, username="someone"),
        message_id=1,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        user_id="42",
        user_name="someone",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            user_id="42",
            user_name="someone",
        ),
        channel_prompt="",
        raw_message=raw,
        message_id="1",
    )


def test_identity_prompt_injected_when_observe_unmentioned_off():
    """Default config (observe_unmentioned_group_messages=False) must still
    inject the bot identity hint into channel_prompt. Otherwise the agent
    cannot recognize ``@<bot>`` mentions as addressed to itself.
    """
    adapter = _make_adapter(bot_username="myninamaxbot", observe_unmentioned=False)
    event = _make_event(chat_type="supergroup", text="hello")
    out = adapter._apply_telegram_group_observe_attribution(event)
    assert "myninamaxbot" in out.channel_prompt, (
        "channel_prompt must include the bot's @-mention so the agent can "
        "recognize mentions addressed to it"
    )


def test_identity_prompt_injected_when_observe_unmentioned_on():
    """When observe_unmentioned_group_messages=True AND the chat is in the
    observe-allowlist, both the identity hint AND the observe-attribution
    block must appear in channel_prompt.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {
        "allowed_chats": ["-1001"],
        "group_allowed_chats": ["-1001"],
        "allowed_topics": [],
        "mention_patterns": [],
        "observe_unmentioned_group_messages": True,
    }
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(
        id=999, username="myninamaxbot", get_me=AsyncMock()
    )
    adapter._bot_username_observed = "myninamaxbot"
    adapter._bot_identity_checked_at = 0.0
    adapter._webhook_mode = False

    event = _make_event(chat_type="supergroup", text="hello")
    out = adapter._apply_telegram_group_observe_attribution(event)
    assert "myninamaxbot" in out.channel_prompt
    # And the observe-attribution block also fires (because chat is in observe allowlist).
    assert "observed Telegram group context" in out.channel_prompt


def test_identity_prompt_skipped_for_dm():
    """DMs must NOT receive the group-context identity block — the prompt is
    only for group chats where the bot's @-mention is meaningful.
    """
    adapter = _make_adapter(bot_username="myninamaxbot")
    event = _make_event(chat_type="private", text="hello")
    out = adapter._apply_telegram_group_observe_attribution(event)
    # DM: no change to channel_prompt.
    assert out.channel_prompt == ""


def test_identity_prompt_includes_handle_hint():
    """The injected prompt must explicitly tell the agent that ``@<bot>``
    mentions addressed to it should be answered.
    """
    adapter = _make_adapter(bot_username="myninamaxbot")
    event = _make_event(chat_type="supergroup", text="@myninamaxbot hi")
    out = adapter._apply_telegram_group_observe_attribution(event)
    # The hint explicitly says the agent should respond to mentions addressed
    # to it, even when the leading handle was stripped from the message text.
    assert "treat it as" in out.channel_prompt.lower()
    assert "addressed to you" in out.channel_prompt.lower()


def test_mention_strip_does_not_lose_intent():
    """End-to-end: when a user types ``@myninamaxbot hello`` in a group, the
    agent receives:
      - event.text = "hello" (mention stripped, as before)
      - event.channel_prompt = "...@myninamaxbot...addressed to you..."
    So the model can recover the intent ("hello" was addressed to me).
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = _make_adapter(bot_username="myninamaxbot")

    # Mimic _clean_bot_trigger_text output: "hello" instead of "@myninamaxbot hello"
    raw_text = "@myninamaxbot hello"
    cleaned = adapter._clean_bot_trigger_text(raw_text)
    assert cleaned == "hello"  # confirm the strip is what we expect

    event = _make_event(chat_type="supergroup", text=cleaned)
    out = adapter._apply_telegram_group_observe_attribution(event)

    # Strip removed the mention; identity prompt carries it instead.
    assert "myninamaxbot" not in out.text
    assert "myninamaxbot" in out.channel_prompt
