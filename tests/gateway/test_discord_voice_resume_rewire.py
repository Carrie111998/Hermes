"""Regression tests for voice restoration after a gateway RESUME or reconnect.

Two distinct failures hide behind an all-green adapter:

* A RESUMED session can re-establish voice UDP state — secret key, ssrc,
  socket reader — underneath a running ``VoiceReceiver``. The receiver
  captures those once at ``start()``, so it goes silently deaf.
* A resume or full reconnect can drop the voice connection outright. The
  guild vanishes from ``_voice_clients``, so any sweep over connected
  clients is a silent no-op and the bot stays out of the channel until a
  restart. Observed in production as 2.5 days out of voice behind green
  health signals.

The adapter must remember where it MEANT to be (``_voice_channel_intents``)
and, after ``on_resumed`` or a repeat ``on_ready`` (a full reconnect),
rewire receivers that survived and rejoin channels that did not.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# Re-use the shared discord-stub bootstrap and FakeBot from the connect
# test module so this file doesn't duplicate the (large) mock surface.
from tests.gateway.test_discord_connect import (  # noqa: E402
    FakeBot,
    _ensure_discord_mock,
)

_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _make_adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))


def _fake_vc(connected: bool = True) -> SimpleNamespace:
    return SimpleNamespace(is_connected=lambda: connected)


def _fake_receiver() -> Mock:
    receiver = Mock()
    receiver.calls = []
    receiver.stop.side_effect = lambda: receiver.calls.append("stop")
    receiver.start.side_effect = lambda: receiver.calls.append("start")
    return receiver


def _fake_channel(channel_id: int, guild_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id, guild=SimpleNamespace(id=guild_id))


async def _connect(adapter: DiscordAdapter, monkeypatch, bot_factory=FakeBot):
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock", lambda scope, identity: None
    )
    intents = SimpleNamespace(
        message_content=False,
        dm_messages=False,
        guild_messages=False,
        members=False,
        voice_states=False,
    )
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)
    monkeypatch.setattr(discord_platform.commands, "Bot", bot_factory)
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())
    assert await adapter.connect() is True


@pytest.mark.asyncio
async def test_on_resumed_is_registered_and_schedules_restore(monkeypatch):
    """``on_resumed`` must exist and must run the restore coroutine."""
    adapter = _make_adapter()
    await _connect(adapter, monkeypatch)

    handler = adapter._client._events.get("on_resumed")
    assert handler is not None, "adapter must register an on_resumed handler"

    restore = AsyncMock()
    monkeypatch.setattr(adapter, "_restore_voice_after_reconnect", restore)
    await handler()
    await asyncio.sleep(0)
    restore.assert_awaited_once()
    # The scheduled task is retained so it cannot be garbage-collected.
    assert adapter._voice_restore_task is not None


@pytest.mark.asyncio
async def test_repeat_on_ready_schedules_restore_but_first_does_not(monkeypatch):
    """A repeat on_ready is a full reconnect and must restore voice too."""
    adapter = _make_adapter()
    restore = AsyncMock()
    monkeypatch.setattr(adapter, "_restore_voice_after_reconnect", restore)

    await _connect(adapter, monkeypatch)
    handler = adapter._client._events.get("on_ready")
    assert handler is not None

    # _connect fired the genuine first on_ready, so rewind the flag to
    # exercise the initial-connect branch explicitly.
    adapter._saw_ready = False
    await handler()  # first ready: initial connect, no restore
    await asyncio.sleep(0)
    restore.assert_not_awaited()

    await handler()  # second ready: full reconnect
    await asyncio.sleep(0)
    restore.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_rewires_receiver_for_connected_guild():
    """An intended guild whose client survived gets its receiver restarted."""
    adapter = _make_adapter()
    receiver = _fake_receiver()
    adapter._voice_channel_intents[123] = 456
    adapter._voice_clients[123] = _fake_vc(connected=True)
    adapter._voice_receivers[123] = receiver

    await adapter._restore_voice_after_reconnect(settle_seconds=0)

    assert receiver.calls == ["stop", "start"]


@pytest.mark.asyncio
async def test_restore_rejoins_dropped_guild_preserving_bindings(monkeypatch):
    """A dropped connection is rejoined via leave+join with routing kept.

    This is THE production case: the resume emptied ``_voice_clients``, so
    the old rewire sweep had nothing to iterate and the bot stayed out of
    the channel for days. The intent record is what brings it back.
    """
    adapter = _make_adapter()
    adapter._voice_channel_intents[123] = 456
    adapter._voice_text_channels[123] = 789
    adapter._voice_sources[123] = {"chat_id": "789"}
    channel = _fake_channel(456, 123)
    adapter._client = SimpleNamespace(get_channel=lambda cid: channel)

    leave = AsyncMock()
    join = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "leave_voice_channel", leave)
    monkeypatch.setattr(adapter, "join_voice_channel", join)

    await adapter._restore_voice_after_reconnect(settle_seconds=0)

    leave.assert_awaited_once_with(123)
    join.assert_awaited_once_with(
        channel, text_channel_id=789, source={"chat_id": "789"}
    )


@pytest.mark.asyncio
async def test_restore_rejoins_guild_with_disconnected_client(monkeypatch):
    """A present-but-disconnected client is treated as dropped, not skipped."""
    adapter = _make_adapter()
    adapter._voice_channel_intents[123] = 456
    adapter._voice_clients[123] = _fake_vc(connected=False)
    channel = _fake_channel(456, 123)
    adapter._client = SimpleNamespace(get_channel=lambda cid: channel)

    join = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "leave_voice_channel", AsyncMock())
    monkeypatch.setattr(adapter, "join_voice_channel", join)

    await adapter._restore_voice_after_reconnect(settle_seconds=0)

    join.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_rejoin_keeps_the_intent_for_the_next_attempt(monkeypatch):
    """A failed rejoin must not erase where the bot meant to be."""
    adapter = _make_adapter()
    adapter._voice_channel_intents[123] = 456
    channel = _fake_channel(456, 123)
    adapter._client = SimpleNamespace(get_channel=lambda cid: channel)

    monkeypatch.setattr(adapter, "leave_voice_channel", AsyncMock())
    monkeypatch.setattr(
        adapter, "join_voice_channel", AsyncMock(return_value=False)
    )

    await adapter._restore_voice_after_reconnect(settle_seconds=0)

    assert adapter._voice_channel_intents.get(123) == 456


@pytest.mark.asyncio
async def test_rejoin_failure_in_one_guild_does_not_block_others():
    """An exception restoring one guild must not stop the sweep."""
    adapter = _make_adapter()
    receiver = _fake_receiver()
    adapter._voice_channel_intents[1] = 10  # will blow up resolving
    adapter._voice_channel_intents[2] = 20  # connected, should still rewire
    adapter._voice_clients[2] = _fake_vc(connected=True)
    adapter._voice_receivers[2] = receiver

    def _boom(cid):
        raise RuntimeError("boom")

    adapter._client = SimpleNamespace(get_channel=_boom)

    await adapter._restore_voice_after_reconnect(settle_seconds=0)

    assert receiver.calls == ["stop", "start"]
    # The failed guild keeps its intent for the next reconnect.
    assert adapter._voice_channel_intents.get(1) == 10


@pytest.mark.asyncio
async def test_deliberate_leave_clears_the_intent():
    """After leave_voice_channel the restore path must not rejoin."""
    adapter = _make_adapter()
    adapter._voice_channel_intents[123] = 456
    adapter._client = SimpleNamespace(
        get_channel=lambda cid: pytest.fail(
            "restore must not resolve a channel after a deliberate leave"
        ),
        get_guild=lambda gid: None,
    )

    await adapter.leave_voice_channel(123)
    assert 123 not in adapter._voice_channel_intents

    await adapter._restore_voice_after_reconnect(settle_seconds=0)


@pytest.mark.asyncio
async def test_restore_with_no_intents_is_a_noop():
    """No intents, no work — and definitely no exception."""
    adapter = _make_adapter()
    await adapter._restore_voice_after_reconnect(settle_seconds=0)


@pytest.mark.asyncio
async def test_schedule_does_not_stack_restores(monkeypatch):
    """A restore already in flight is not stacked with a second one."""
    adapter = _make_adapter()
    started = 0
    release = asyncio.Event()

    async def _slow_restore(settle_seconds: float = 5.0):
        nonlocal started
        started += 1
        await release.wait()

    monkeypatch.setattr(adapter, "_restore_voice_after_reconnect", _slow_restore)

    adapter._schedule_voice_restore()
    await asyncio.sleep(0)
    adapter._schedule_voice_restore()
    await asyncio.sleep(0)
    release.set()
    await adapter._voice_restore_task

    assert started == 1
