"""Tests for Discord message reactions tied to processing lifecycle hooks."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import SessionSource, build_session_key


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
import plugins.platforms.discord.adapter as discord_adapter_module  # noqa: E402


class FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(
        tree=FakeTree(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="HermesBot"),
    )
    return adapter


def _make_event(message_id: str, raw_message) -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            chat_type="dm",
            user_id="42",
            user_name="Jezza",
        ),
        raw_message=raw_message,
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_process_message_background_adds_and_swaps_reactions(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("1", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    assert raw_message.add_reaction.await_args_list[0].args == ("👀",)
    assert raw_message.remove_reaction.await_args_list[0].args == ("👀", adapter._client.user)
    assert raw_message.add_reaction.await_args_list[1].args == ("✅",)


@pytest.mark.asyncio
async def test_interaction_backed_events_do_not_attempt_reactions(adapter):
    interaction = SimpleNamespace(guild_id=123456789)

    async def handler(_event):
        await asyncio.sleep(0)
        return None

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter._add_reaction = AsyncMock()
    adapter._remove_reaction = AsyncMock()
    adapter._keep_typing = hold_typing

    event = MessageEvent(
        text="/status",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            chat_type="dm",
            user_id="42",
            user_name="Jezza",
        ),
        raw_message=interaction,
        message_id="2",
    )

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter._add_reaction.assert_not_awaited()
    adapter._remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_helper_failures_do_not_break_message_flow(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(side_effect=[RuntimeError("no perms"), RuntimeError("no perms")]),
        remove_reaction=AsyncMock(side_effect=RuntimeError("no perms")),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("3", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reactions_disabled_via_env(adapter, monkeypatch):
    """When DISCORD_REACTIONS=false, no reactions should be added."""
    monkeypatch.setenv("DISCORD_REACTIONS", "false")

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("4", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    raw_message.add_reaction.assert_not_awaited()
    raw_message.remove_reaction.assert_not_awaited()
    # Response should still be sent
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reactions_disabled_via_env_zero(adapter, monkeypatch):
    """DISCORD_REACTIONS=0 should also disable reactions."""
    monkeypatch.setenv("DISCORD_REACTIONS", "0")

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("5", raw_message)
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    raw_message.add_reaction.assert_not_awaited()
    raw_message.remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_reactions_enabled_by_default(adapter, monkeypatch):
    """When DISCORD_REACTIONS is unset, reactions should still work (default: true)."""
    monkeypatch.delenv("DISCORD_REACTIONS", raising=False)

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("6", raw_message)
    await adapter.on_processing_start(event)

    raw_message.add_reaction.assert_awaited_once_with("👀")


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_removes_eyes_without_terminal_reaction(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("7", raw_message)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    raw_message.remove_reaction.assert_awaited_once_with("👀", adapter._client.user)
    raw_message.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_tool_progress_reaction_stages_tool_emoji(adapter):
    raw_message = SimpleNamespace(id=123, add_reaction=AsyncMock())

    assert await adapter.add_tool_progress_reaction(raw_message, "📋") is True
    raw_message.add_reaction.assert_not_awaited()
    assert adapter._tool_progress_reactions["123"] == ["📋"]


@pytest.mark.asyncio
async def test_add_tool_progress_reaction_by_id_stages_without_fetch(adapter):
    assert await adapter.add_tool_progress_reaction_by_id("123", "456", "📋") is True
    adapter._client.fetch_channel.assert_not_awaited()
    assert adapter._tool_progress_reactions["456"] == ["📋"]


@pytest.mark.asyncio
async def test_final_tool_reactions_are_deduped_primary_emojis(adapter):
    raw_message = SimpleNamespace(id=123, add_reaction=AsyncMock(), remove_reaction=AsyncMock())
    event = _make_event("123", raw_message)
    await adapter.on_processing_start(event)

    for _ in range(4):
        assert await adapter.add_tool_progress_reaction(raw_message, "💻") is True
    assert await adapter.add_tool_progress_reaction(raw_message, "📖") is True
    assert await adapter.add_tool_progress_reaction(raw_message, "💻") is True

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args[0] for call in raw_message.add_reaction.await_args_list] == [
        "👀", "💻", "📖", "✅"
    ]
    raw_message.remove_reaction.assert_awaited_once_with("👀", adapter._client.user)


@pytest.mark.asyncio
async def test_live_tool_count_embed_is_edited_then_deleted_before_final_reactions(adapter, monkeypatch):
    class FakeEmbed:
        def __init__(self, *, description, colour, title=None):
            self.title = title
            self.description = description
            self.colour = colour
            self.footer = None

    monkeypatch.setattr(discord_adapter_module, "_DISCORD_TOOL_STATUS_RENDER_DELAY_SECONDS", 0)
    monkeypatch.setattr(discord_adapter_module, "discord", SimpleNamespace(Embed=FakeEmbed))
    monkeypatch.setattr(discord_adapter_module, "DISCORD_AVAILABLE", True)
    status_message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=status_message))
    raw_message = SimpleNamespace(
        id=123,
        channel=channel,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("123", raw_message)
    await adapter.on_processing_start(event)

    assert await adapter.add_tool_progress_reaction(raw_message, "💻") is True
    await adapter._tool_progress_status_tasks["123"]
    channel.send.assert_awaited_once()
    first_embed = channel.send.await_args.kwargs["embed"]
    assert first_embed.description == "💻 ×1"
    assert first_embed.title is None
    assert first_embed.footer is None

    assert await adapter.add_tool_progress_reaction(raw_message, "💻") is True
    assert await adapter.add_tool_progress_reaction(raw_message, "📖") is True
    await adapter._tool_progress_status_tasks["123"]
    status_message.edit.assert_awaited_once()
    assert status_message.edit.await_args.kwargs["embed"].description == "💻 ×2 · 📖 ×1"

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    status_message.delete.assert_awaited_once()
    assert [call.args[0] for call in raw_message.add_reaction.await_args_list] == [
        "👀", "💻", "📖", "✅"
    ]


@pytest.mark.asyncio
async def test_long_running_status_is_embed_edited_then_deleted(adapter, monkeypatch):
    class FakeEmbed:
        def __init__(self, *, description, colour, title=None):
            self.description = description
            self.colour = colour
            self.title = title

    monkeypatch.setattr(discord_adapter_module, "discord", SimpleNamespace(Embed=FakeEmbed))
    monkeypatch.setattr(discord_adapter_module, "DISCORD_AVAILABLE", True)
    status_message = SimpleNamespace(id=456, edit=AsyncMock(), delete=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=status_message))
    adapter._client.get_channel = lambda _id: channel
    raw_message = SimpleNamespace(id=123, add_reaction=AsyncMock(), remove_reaction=AsyncMock())
    event = _make_event("123", raw_message)
    await adapter.on_processing_start(event)

    first = await adapter.render_long_running_status(
        "123", "⏳ Working — 3 min", origin_message_id="123"
    )
    second = await adapter.render_long_running_status(
        "123", "⏳ Working — 6 min", origin_message_id="123", message_id=first.message_id
    )

    assert first.success and second.success
    assert first.message_id == "456"
    assert channel.send.await_args.kwargs["embed"].description == "⏳ Working — 3 min"
    assert status_message.edit.await_args.kwargs["embed"].description == "⏳ Working — 6 min"
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    status_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_transient_statuses_deletes_all_active_turn_artifacts(adapter):
    tool_status = SimpleNamespace(delete=AsyncMock())
    heartbeat_status = SimpleNamespace(delete=AsyncMock())
    adapter._tool_progress_status_messages["turn-a"] = tool_status
    adapter._tool_progress_counts["turn-a"] = {"💻": 1}
    adapter._tool_progress_channels["turn-a"] = SimpleNamespace()
    adapter._long_running_status_messages["turn-b"] = heartbeat_status

    await adapter._cleanup_transient_statuses()

    tool_status.delete.assert_awaited_once()
    heartbeat_status.delete.assert_awaited_once()
    assert adapter._tool_progress_status_messages == {}
    assert adapter._tool_progress_counts == {}
    assert adapter._tool_progress_channels == {}
    assert adapter._long_running_status_messages == {}


@pytest.mark.asyncio
async def test_resolve_clarify_text_response_marks_open_prompt_resolved(adapter, monkeypatch):
    class FakeEmbed:
        def __init__(self):
            self.color = None
            self.footer_text = None

        def set_footer(self, *, text):
            self.footer_text = text

    monkeypatch.setattr(
        discord_adapter_module,
        "discord",
        SimpleNamespace(Color=SimpleNamespace(green=lambda: "green")),
    )
    prompt = SimpleNamespace(embeds=[FakeEmbed()], edit=AsyncMock())
    adapter._clarify_messages["clarify-1"] = prompt

    assert await adapter.resolve_clarify_text_response("clarify-1", "Ship it") is True
    assert prompt.embeds[0].color == "green"
    assert prompt.embeds[0].footer_text == "Answered: Ship it"
    prompt.edit.assert_awaited_once_with(embed=prompt.embeds[0], view=None)
    assert adapter._clarify_messages == {}


@pytest.mark.asyncio
async def test_add_tool_progress_reaction_respects_disabled_reactions(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REACTIONS", "false")
    raw_message = SimpleNamespace(add_reaction=AsyncMock())

    assert await adapter.add_tool_progress_reaction(raw_message, "✍️") is False
    raw_message.add_reaction.assert_not_awaited()
