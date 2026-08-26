"""Behavior contracts for visible Discord handoff threads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
import plugins.platforms.discord.adapter as discord_adapter_module


PARENT_ID = "123456789"
THREAD_REASON = "Hermes session handoff"


def _adapter_with_parent(parent):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._client = MagicMock()
    adapter._client.get_channel.return_value = parent
    return adapter


@pytest.mark.asyncio
async def test_handoff_posts_one_visible_anchor_before_message_thread_creation(
    monkeypatch,
):
    events = []
    thread = SimpleNamespace(id=987654321)
    safe_mentions = object()
    monkeypatch.setattr(
        discord_adapter_module.discord.AllowedMentions,
        "none",
        lambda: safe_mentions,
    )

    async def send_anchor(content, **kwargs):
        events.append(("send", content, kwargs))
        return seed_message

    async def create_from_anchor(**kwargs):
        events.append(("create", kwargs))
        return thread

    seed_message = SimpleNamespace(
        create_thread=AsyncMock(side_effect=create_from_anchor)
    )
    parent = SimpleNamespace(
        send=AsyncMock(side_effect=send_anchor),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)
    adapter._threads.mark = MagicMock(
        side_effect=lambda thread_id: events.append(("mark", thread_id))
    )

    result = await adapter.create_handoff_thread(PARENT_ID, "Daily writing prompt")

    assert result == "987654321"
    assert events == [
        (
            "send",
            "\N{SPOOL OF THREAD} **Daily writing prompt** \N{EM DASH} "
            "open this thread to continue.",
            {"allowed_mentions": safe_mentions},
        ),
        (
            "create",
            {
                "name": "Daily writing prompt",
                "auto_archive_duration": 1440,
                "reason": THREAD_REASON,
            },
        ),
        ("mark", "987654321"),
    ]
    adapter._threads.mark.assert_called_once_with(result)
    parent.send.assert_awaited_once()
    seed_message.create_thread.assert_awaited_once()
    parent.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_anchor_and_thread_name_are_sanitized_and_limited():
    raw_name = f"  {'x' * 90}   <@123456789>  "
    expected_name = f"{'x' * 77}..."
    seed_message = SimpleNamespace(
        create_thread=AsyncMock(return_value=SimpleNamespace(id=246813579))
    )
    parent = SimpleNamespace(
        send=AsyncMock(return_value=seed_message),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)

    result = await adapter.create_handoff_thread(PARENT_ID, raw_name)

    assert result == "246813579"
    assert parent.send.await_args.args[0] == (
        f"\N{SPOOL OF THREAD} **{expected_name}** \N{EM DASH} "
        "open this thread to continue."
    )
    seed_message.create_thread.assert_awaited_once_with(
        name=expected_name,
        auto_archive_duration=1440,
        reason=THREAD_REASON,
    )
    parent.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_thread_name_respects_utf16_limit_for_emoji():
    from gateway.platforms.base import utf16_len

    seed_message = SimpleNamespace(
        create_thread=AsyncMock(return_value=SimpleNamespace(id=135792468))
    )
    parent = SimpleNamespace(
        send=AsyncMock(return_value=seed_message),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)

    result = await adapter.create_handoff_thread(PARENT_ID, "\N{ROBOT FACE}" * 50)

    assert result == "135792468"
    created_name = seed_message.create_thread.await_args.kwargs["name"]
    assert utf16_len(created_name) <= 80
    assert created_name.endswith("...")
    parent.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_anchor_send_failure_returns_none_without_direct_thread():
    parent = SimpleNamespace(
        send=AsyncMock(side_effect=PermissionError("cannot send anchor")),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)
    adapter._threads.mark = MagicMock()

    result = await adapter.create_handoff_thread(PARENT_ID, "Daily writing prompt")

    assert result is None
    parent.send.assert_awaited_once()
    parent.create_thread.assert_not_awaited()
    adapter._threads.mark.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_anchor_without_create_method_never_marks_participation():
    parent = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace()),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)
    adapter._threads.mark = MagicMock()

    result = await adapter.create_handoff_thread(PARENT_ID, "Daily writing prompt")

    assert result is None
    parent.send.assert_awaited_once()
    parent.create_thread.assert_not_awaited()
    adapter._threads.mark.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_anchor_thread_failure_returns_none_and_leaves_terse_orphan():
    seed_message = SimpleNamespace(
        create_thread=AsyncMock(side_effect=PermissionError("cannot create thread"))
    )
    parent = SimpleNamespace(
        send=AsyncMock(return_value=seed_message),
        create_thread=AsyncMock(side_effect=AssertionError("unanchored thread path")),
    )
    adapter = _adapter_with_parent(parent)
    adapter._threads.mark = MagicMock()

    result = await adapter.create_handoff_thread(PARENT_ID, "Daily writing prompt")

    assert result is None
    assert parent.send.await_args.args[0] == (
        "\N{SPOOL OF THREAD} **Daily writing prompt** \N{EM DASH} "
        "open this thread to continue."
    )
    seed_message.create_thread.assert_awaited_once()
    parent.create_thread.assert_not_awaited()
    adapter._threads.mark.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_invalid_parent_id_never_marks_participation():
    adapter = _adapter_with_parent(SimpleNamespace())
    adapter._threads.mark = MagicMock()

    result = await adapter.create_handoff_thread("not-a-snowflake", "Daily writing prompt")

    assert result is None
    client = adapter._client
    assert client is not None
    client.get_channel.assert_not_called()
    adapter._threads.mark.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("discord_parent_type", ["DMChannel", "VoiceChannel", "Thread"])
async def test_handoff_rejects_unsupported_parent_types(
    monkeypatch, discord_parent_type
):
    class UnsupportedParent:
        def __init__(self):
            self.send = AsyncMock()
            self.create_thread = AsyncMock()

    monkeypatch.setattr(
        discord_adapter_module.discord,
        discord_parent_type,
        UnsupportedParent,
    )
    parent = UnsupportedParent()
    adapter = _adapter_with_parent(parent)
    adapter._threads.mark = MagicMock()

    result = await adapter.create_handoff_thread(PARENT_ID, "Daily writing prompt")

    assert result is None
    parent.send.assert_not_awaited()
    parent.create_thread.assert_not_awaited()
    adapter._threads.mark.assert_not_called()
