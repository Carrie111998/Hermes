"""Regression tests: Discord send_clarify reply-anchors the triggering message.

The gateway now passes ``reply_to_message_id`` + ``notify`` in clarify
metadata (see gateway/run.py). The Discord adapter must build a Discord
MessageReference from that anchor so the user actually gets a notification —
a bare channel message with no reply reference does not ping the author.

These tests spy on ``_reply_reference_for_send`` (the same pre-existing
helper ``send()`` uses) and assert ``send_clarify`` wires the anchor through
to it, rather than asserting on ``discord.MessageReference`` internals (the
test env mocks discord.py, whose mock discards kwargs).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.e2e.conftest import (
    make_fake_text_channel,
)

pytestmark = pytest.mark.asyncio


def _make_client_with_channel(channel):
    """Discord-like client whose get_channel returns the given channel."""
    return SimpleNamespace(
        user=SimpleNamespace(id=99999, name="HermesBot", display_name="HermesBot", bot=True),
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(),
    )


def _patch_view_class(adapter_module):
    """Replace ClarifyChoiceView with a plain dummy.

    The test env mocks discord.py; constructing the real view class through
    the mock's discord.ui.View machinery is flaky (exhausts a mock
    side_effect on repeat construction). The view's internals are not what
    this test covers.
    """
    return patch.object(adapter_module, "ClarifyChoiceView", SimpleNamespace)


class TestDiscordSendClarifyReplyAnchor:
    async def test_choice_clarify_reply_references_triggering_message(self, discord_adapter):
        """metadata.reply_to_message_id → _reply_reference_for_send(anchor) result passed as reference=."""
        import plugins.platforms.discord.adapter as da

        channel = make_fake_text_channel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=12345))
        discord_adapter._client = _make_client_with_channel(channel)

        sentinel = object()
        with _patch_view_class(da), patch.object(
            discord_adapter, "_reply_reference_for_send", return_value=sentinel
        ) as spy:
            result = await discord_adapter.send_clarify(
                "22222", "Pick one", ["A", "B"], "clarify-1", "sk",
                metadata={"reply_to_message_id": "70001", "notify": True},
            )

        assert result.success
        spy.assert_called_once_with("70001", channel)
        assert channel.send.await_args.kwargs["reference"] is sentinel

    async def test_open_ended_clarify_reply_references_triggering_message(self, discord_adapter):
        """No choices (open-ended) → still passes the reply reference."""
        import plugins.platforms.discord.adapter as da

        channel = make_fake_text_channel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=12346))
        discord_adapter._client = _make_client_with_channel(channel)

        sentinel = object()
        with patch.object(
            discord_adapter, "_reply_reference_for_send", return_value=sentinel
        ) as spy:
            result = await discord_adapter.send_clarify(
                "22222", "Tell me more", None, "clarify-2", "sk",
                metadata={"reply_to_message_id": "70002", "notify": True},
            )

        assert result.success
        spy.assert_called_once_with("70002", channel)
        assert channel.send.await_args.kwargs["reference"] is sentinel

    async def test_no_anchor_no_reference(self, discord_adapter):
        """Without reply_to_message_id metadata → no reference (old behavior)."""
        import plugins.platforms.discord.adapter as da

        channel = make_fake_text_channel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=12347))
        discord_adapter._client = _make_client_with_channel(channel)

        with _patch_view_class(da), patch.object(
            discord_adapter, "_reply_reference_for_send", return_value=object()
        ) as spy:
            result = await discord_adapter.send_clarify(
                "22222", "Pick one", ["A"], "clarify-3", "sk", metadata=None,
            )

        assert result.success
        spy.assert_not_called()
        assert channel.send.await_args.kwargs["reference"] is None

    async def test_reply_to_mode_off_suppresses_reference(self, discord_adapter):
        """reply_to_mode 'off' must suppress the anchor like send() does.

        Uses the REAL _reply_reference_for_send (not a spy) — the off check
        lives inside that method, so replacing it would bypass the behavior
        under test.
        """
        import plugins.platforms.discord.adapter as da

        channel = make_fake_text_channel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=12348))
        discord_adapter._client = _make_client_with_channel(channel)
        discord_adapter._reply_to_mode = "off"

        with _patch_view_class(da):
            result = await discord_adapter.send_clarify(
                "22222", "Pick one", ["A"], "clarify-4", "sk",
                metadata={"reply_to_message_id": "70004", "notify": True},
            )

        assert result.success
        assert channel.send.await_args.kwargs["reference"] is None
