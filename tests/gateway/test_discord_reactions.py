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
async def test_discord_run_lifecycle_sends_start_then_response_then_fresh_terminal_message(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("8", raw_message)
    event.source.chat_type = "thread"
    event.source.thread_id = "456"
    session_key = build_session_key(event.source)

    async def handler(current_event):
        await adapter.begin_run_lifecycle(
            current_event,
            session_key=session_key,
            generation=3,
        )

        async def deferred_delivery():
            await adapter.send(
                current_event.source.chat_id,
                "deferred output",
                metadata={"thread_id": "456"},
            )

        adapter.register_post_delivery_callback(
            session_key,
            deferred_delivery,
            generation=3,
        )
        return "answer"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="start"),
            SendResult(success=True, message_id="answer"),
            SendResult(success=True, message_id="deferred"),
            SendResult(success=True, message_id="terminal"),
        ]
    )
    adapter._keep_typing = hold_typing

    await adapter._process_message_background(event, session_key)

    sent = adapter.send.await_args_list
    contents = [
        call.args[1] if len(call.args) > 1 else call.kwargs["content"]
        for call in sent
    ]
    assert contents[:3] == ["⏳ Run started", "answer", "deferred output"]
    assert contents[3].startswith("✅ Run complete · ")
    assert sent[0].kwargs["metadata"]["thread_id"] == "456"
    assert sent[3].kwargs["metadata"]["thread_id"] == "456"
    assert sent[0].kwargs["metadata"]["non_conversational"] is True
    assert sent[3].kwargs["metadata"]["non_conversational"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("processing_outcome", "lifecycle_outcome", "prefix"),
    [
        (ProcessingOutcome.SUCCESS, "timeout", "⚠️ Run timed out · "),
        (ProcessingOutcome.FAILURE, None, "❌ Run failed · "),
        (ProcessingOutcome.CANCELLED, None, "⏹️ Run stopped · "),
    ],
)
async def test_discord_run_lifecycle_maps_terminal_outcomes(
    adapter,
    processing_outcome,
    lifecycle_outcome,
    prefix,
):
    event = _make_event("9", SimpleNamespace(add_reaction=AsyncMock(), remove_reaction=AsyncMock()))
    event.source.chat_type = "thread"
    event.source.thread_id = "456"
    session_key = build_session_key(event.source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="status"))

    await adapter.begin_run_lifecycle(event, session_key=session_key, generation=4)
    if lifecycle_outcome:
        adapter.set_run_lifecycle_outcome(event, lifecycle_outcome)
    await adapter.on_processing_complete(event, processing_outcome)
    callback = adapter.pop_post_delivery_callback(session_key, generation=4)

    assert callback is not None
    await callback()
    await callback()

    terminal_contents = [
        call.args[1]
        for call in adapter.send.await_args_list
        if call.args[1] != "⏳ Run started"
    ]
    assert len(terminal_contents) == 1
    assert terminal_contents[0].startswith(prefix)


@pytest.mark.asyncio
async def test_discord_run_terminal_precedes_queued_followup_start(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    first = _make_event("10", raw_message)
    first.source.chat_type = "thread"
    first.source.thread_id = "456"
    second = _make_event("11", raw_message)
    second.source.chat_type = "thread"
    second.source.thread_id = "456"
    session_key = build_session_key(first.source)
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    turns = 0

    async def handler(current_event):
        nonlocal turns
        turns += 1
        generation = turns
        interrupt_event = adapter._active_sessions[session_key]
        setattr(interrupt_event, "_hermes_run_generation", generation)
        await adapter.begin_run_lifecycle(
            current_event,
            session_key=session_key,
            generation=generation,
        )
        if turns == 1:
            async def deferred_delivery():
                await adapter.send(
                    current_event.source.chat_id,
                    "first deferred output",
                    metadata={"thread_id": "456"},
                )

            adapter.register_post_delivery_callback(
                session_key,
                deferred_delivery,
                generation=generation,
            )
            adapter._pending_messages[session_key] = second
            return "first answer"
        second_started.set()
        await release_second.wait()
        return "second answer"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="message"))
    adapter._keep_typing = hold_typing

    await adapter._process_message_background(first, session_key)
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    release_second.set()
    task = adapter._session_tasks.get(session_key)
    if task is not None:
        await asyncio.wait_for(task, timeout=1.0)

    contents = [
        call.args[1] if len(call.args) > 1 else call.kwargs["content"]
        for call in adapter.send.await_args_list
    ]
    second_start_index = contents.index("⏳ Run started", 1)
    first_terminal_indices = [
        index
        for index, text in enumerate(contents)
        if text.startswith("✅ Run complete · ") and index < second_start_index
    ]
    assert contents[:3] == [
        "⏳ Run started",
        "first answer",
        "first deferred output",
    ]
    assert first_terminal_indices == [3]
