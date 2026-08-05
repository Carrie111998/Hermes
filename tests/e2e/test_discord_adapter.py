"""Minimal e2e tests for Discord mention stripping + /command detection.

Covers the fix for slash commands not being recognized when sent via
@mention in a channel, especially after auto-threading.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.session import build_session_key
from tests.e2e.conftest import (
    BOT_USER_ID,
    CHANNEL_ID,
    E2E_MESSAGE_SETTLE_DELAY,
    get_response_text,
    make_discord_message,
    make_fake_dm_channel,
    make_fake_text_channel,
    make_fake_thread,
)

pytestmark = pytest.mark.asyncio


async def dispatch(adapter, msg):
    await adapter._handle_message(msg)
    await asyncio.sleep(E2E_MESSAGE_SETTLE_DELAY)


class TestMentionStrippedCommandDispatch:
    async def test_mention_then_command(self, discord_adapter, bot_user):
        """<@BOT> /help → mention stripped, /help dispatched."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_nickname_mention_then_command(self, discord_adapter, bot_user):
        """<@!BOT> /help → nickname mention also stripped, /help works."""
        msg = make_discord_message(
            content=f"<@!{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_text_before_command_not_detected(self, discord_adapter, bot_user):
        """'<@BOT> something else /help' → mention stripped, but 'something else /help'
        doesn't start with / so it's treated as text, not a command."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> something else /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # Message is accepted (not dropped by mention gate), but since it doesn't
        # start with / it's routed as text — no command output, and no agent in this
        # mock setup means no send call either.
        response = get_response_text(discord_adapter)
        assert response is None or "/new" not in response

    async def test_no_mention_in_channel_dropped(self, discord_adapter):
        """Message without @mention in server channel → silently dropped."""
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_dm_no_mention_needed(self, discord_adapter):
        """DMs don't require @mention — /help works directly."""
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/help", channel=dm, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


class TestAutoThreadingPreservesCommand:
    async def test_command_detected_after_auto_thread(self, discord_adapter, bot_user, monkeypatch):
        """@mention /help in channel with auto-thread → thread created AND command dispatched."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        fake_thread = make_fake_thread(thread_id=90001, name="help")
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )

        # Simulate discord.py restoring the original raw content (with mention)
        # after create_thread(), which undoes any prior mention stripping.
        original_content = msg.content

        async def clobber_content(**kwargs):
            msg.content = original_content
            return fake_thread

        msg.create_thread = AsyncMock(side_effect=clobber_content)
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_awaited_once()
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_successful_auto_thread_routes_event_to_new_thread(
        self, discord_adapter, bot_user, monkeypatch
    ):
        """Successful auto-threading should build the event as a thread turn."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")
        discord_adapter._text_batch_delay_seconds = 0
        discord_adapter.handle_message = AsyncMock()

        parent = make_fake_text_channel(channel_id=CHANNEL_ID, name="support")
        fake_thread = make_fake_thread(thread_id=90002, name="help", parent=parent)
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> help me",
            channel=parent,
            mentions=[bot_user],
        )
        msg.create_thread = AsyncMock(return_value=fake_thread)

        handled = await discord_adapter._handle_message(msg)

        assert handled is True
        msg.create_thread.assert_awaited_once()
        discord_adapter.handle_message.assert_awaited_once()
        event = discord_adapter.handle_message.await_args.args[0]
        assert event.source.chat_id == "90002"
        assert event.source.chat_type == "thread"
        assert event.source.thread_id == "90002"
        assert event.source.prospective_thread_id is None
        assert event.source.parent_chat_id == str(CHANNEL_ID)
        assert event.source.auto_thread_created is True
        assert event.source.auto_thread_initial_name == "help me"

    async def test_failed_auto_thread_continues_in_parent_without_failure_notice(
        self, discord_adapter, bot_user, monkeypatch
    ):
        """If auto-threading fails, the original request should still dispatch in the parent channel."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        monkeypatch.setenv("DISCORD_AUTO_THREAD_FAILURE_MODE", "inline")
        monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")
        discord_adapter._text_batch_delay_seconds = 0
        discord_adapter.handle_message = AsyncMock()

        parent = make_fake_text_channel(channel_id=CHANNEL_ID, name="support")
        parent.send = AsyncMock(side_effect=RuntimeError("seed message failed"))
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> help me",
            channel=parent,
            mentions=[bot_user],
        )
        msg.create_thread = AsyncMock(side_effect=RuntimeError("thread create failed"))

        handled = await discord_adapter._handle_message(msg)

        assert handled is True
        assert msg.create_thread.await_count == 2
        assert parent.send.await_count == 2
        assert all(
            "Hermes could not create a Discord thread" not in call.args[0]
            for call in parent.send.await_args_list
        )
        discord_adapter.handle_message.assert_awaited_once()
        event = discord_adapter.handle_message.await_args.args[0]
        assert event.text == "help me"
        assert event.source.chat_id == str(CHANNEL_ID)
        assert event.source.chat_type == "group"
        assert event.source.thread_id is None
        assert event.source.parent_chat_id is None
        assert event.source.prospective_thread_id == str(msg.id)
        assert build_session_key(event.source) != build_session_key(
            replace(event.source, prospective_thread_id=None)
        )

    async def test_failed_auto_thread_source_metadata_has_no_auto_thread(
        self, discord_adapter, bot_user, monkeypatch
    ):
        """Failure fallback metadata must reflect that no thread was auto-created."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        monkeypatch.setenv("DISCORD_AUTO_THREAD_FAILURE_MODE", "inline")
        monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")
        discord_adapter._text_batch_delay_seconds = 0
        discord_adapter.handle_message = AsyncMock()

        parent = make_fake_text_channel(channel_id=CHANNEL_ID, name="support")
        parent.send = AsyncMock(side_effect=RuntimeError("seed message failed"))
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> summarize this",
            channel=parent,
            mentions=[bot_user],
        )
        msg.create_thread = AsyncMock(side_effect=RuntimeError("thread create failed"))

        handled = await discord_adapter._handle_message(msg)

        assert handled is True
        discord_adapter.handle_message.assert_awaited_once()
        source = discord_adapter.handle_message.await_args.args[0].source
        assert source.auto_thread_created is False
        assert source.auto_thread_initial_name is None
        assert source.prospective_thread_id == str(msg.id)
        metadata = source.to_dict()
        assert "auto_thread_created" not in metadata
        assert "auto_thread_initial_name" not in metadata
        assert metadata["thread_id"] is None
        assert "parent_chat_id" not in metadata
        assert metadata["prospective_thread_id"] == str(msg.id)


class TestRepliedToMediaDispatch:
    async def test_reply_to_image_message_caches_referenced_attachment(
        self, discord_adapter, bot_user, monkeypatch
    ):
        """A text reply to an image-bearing Discord message should give the agent that image."""
        cached_path = "/tmp/replied-discord-image.png"

        async def fake_cache_image_from_url(url, *, ext=".jpg"):
            assert url == "https://cdn.discordapp.com/attachments/image.png"
            assert ext == ".png"
            return cached_path

        monkeypatch.setattr(
            "plugins.platforms.discord.adapter.cache_image_from_url",
            fake_cache_image_from_url,
        )
        discord_adapter.handle_message = AsyncMock()

        attachment = SimpleNamespace(
            content_type="image/png",
            filename="image.png",
            url="https://cdn.discordapp.com/attachments/image.png",
            size=1234,
        )
        referenced_message = SimpleNamespace(
            id=12345,
            content="",
            attachments=[attachment],
        )
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> what's in this image?",
            mentions=[bot_user],
        )
        msg.type = 19
        msg.reference = SimpleNamespace(message_id=12345, resolved=referenced_message)

        await discord_adapter._handle_message(msg)

        discord_adapter.handle_message.assert_awaited_once()
        await_args = discord_adapter.handle_message.await_args
        assert await_args is not None
        event = await_args.args[0]
        assert event.reply_to_message_id == "12345"
        assert event.media_urls == [cached_path]
        assert event.media_types == ["image/png"]
        assert event.message_type.value == "photo"
