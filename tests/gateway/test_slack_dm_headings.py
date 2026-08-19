"""Regression tests for Slack top-level DM heading suppression."""

from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


HEADING_ACK = "見出しとして受信しました。詳細はこのスレッドに送ってください。"
DEFAULT_HEADING_ACK = "Heading received. Send details in this thread."


def make_adapter(extra=None):
    adapter = SlackAdapter(PlatformConfig(enabled=True, extra=extra or {}))
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids["T1"] = "UBOT"
    adapter._has_active_session_for_thread = lambda **_: False
    adapter._fetch_thread_context = AsyncMock(return_value="")
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter._resolve_user_name = AsyncMock(return_value="Sunahara")
    adapter.send = AsyncMock(return_value=type("Result", (), {"success": True, "error": None})())
    return adapter


def slack_event(text, *, ts="100.000", thread_ts_marker=...):
    event = {
        "type": "message",
        "channel": "D123",
        "channel_type": "im",
        "team": "T1",
        "user": "U123",
        "client_msg_id": "client-1",
        "text": text,
        "ts": ts,
    }
    if thread_ts_marker is not ...:
        event["thread_ts"] = thread_ts_marker
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["スレッド", "スレッド Hermes", "スレッド　Hermes", "スレッド、Hermes"])
async def test_top_level_dm_heading_is_acknowledged_without_dispatch(text):
    adapter = make_adapter(
        {
            "dm_heading_prefixes": "スレッド",
            "dm_heading_ack": HEADING_ACK,
        }
    )
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event(text))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "D123",
        HEADING_ACK,
        reply_to="100.000",
        metadata={"thread_id": "100.000", "team_id": "T1"},
    )


@pytest.mark.asyncio
async def test_unconfigured_heading_prefix_does_not_suppress_dispatch():
    adapter = make_adapter()
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("スレッド Hermes"))

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefixes", ["", []])
async def test_empty_heading_prefix_disables_suppression(prefixes):
    adapter = make_adapter({"dm_heading_prefixes": prefixes})
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("スレッド Hermes"))

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_top_level_dm_still_dispatches():
    adapter = make_adapter()
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("通常の依頼"))

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_heading_like_text_without_separator_is_normal_message():
    adapter = make_adapter({"dm_heading_prefixes": "スレッド"})
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("スレッドについて調べて"))

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", ["101.000", "999.000"])
async def test_real_thread_reply_is_not_suppressed_even_when_text_is_heading(thread_ts):
    adapter = make_adapter({"dm_heading_prefixes": "スレッド"})
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(
        slack_event("スレッド Hermes", ts="101.000", thread_ts_marker=thread_ts)
    )

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_ack_is_generic_when_not_configured():
    adapter = make_adapter({"dm_heading_prefixes": "Topic"})
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("Topic Hermes"))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "D123",
        DEFAULT_HEADING_ACK,
        reply_to="100.000",
        metadata={"thread_id": "100.000", "team_id": "T1"},
    )


@pytest.mark.asyncio
async def test_heading_prefix_and_ack_are_configurable():
    adapter = make_adapter(
        {
            "dm_heading_prefixes": ["話題"],
            "dm_heading_ack": "このスレッドで受け付けました。",
        }
    )
    adapter.handle_message = AsyncMock()

    await adapter._handle_slack_message(slack_event("話題 Hermes", ts="200.000"))

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        "D123",
        "このスレッドで受け付けました。",
        reply_to="200.000",
        metadata={"thread_id": "200.000", "team_id": "T1"},
    )

    adapter.handle_message.reset_mock()
    adapter.send.reset_mock()
    await adapter._handle_slack_message(slack_event("スレッド Hermes", ts="201.000"))
    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()
