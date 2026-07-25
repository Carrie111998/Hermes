"""Behavioral tests for opt-in Discord voice-channel auto-join."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, _apply_yaml_config


def _route_config(**overrides):
    route = {
        "guild_id": "100",
        "voice_channel_id": "200",
        "text_channel_id": "300",
        "trigger_user_ids": ["42"],
        "leave_when_no_trigger_users": True,
        "leave_grace_seconds": 0,
    }
    route.update(overrides)
    return {
        "voice_auto_join": {
            "enabled": True,
            "reconnect_on_startup": True,
            "routes": [route],
        }
    }


def _adapter(extra=None):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test", extra=extra or {}))
    adapter._voice_auto_join_callback = AsyncMock(return_value=True)
    adapter._client = MagicMock()
    adapter._client.user = SimpleNamespace(id=999)
    return adapter


def _channel(*, guild_id=100, channel_id=200, members=None, name="Voice"):
    guild = SimpleNamespace(id=guild_id, name="Guild")
    return SimpleNamespace(
        id=channel_id,
        name=name,
        guild=guild,
        members=list(members or []),
    )


def _member(*, user_id=42, guild_id=100, channel=None, bot=False):
    return SimpleNamespace(
        id=user_id,
        display_name=f"user-{user_id}",
        bot=bot,
        guild=SimpleNamespace(id=guild_id),
        voice=SimpleNamespace(channel=channel) if channel is not None else None,
    )


def _state(channel):
    return SimpleNamespace(channel=channel)


def test_auto_join_is_disabled_by_default():
    adapter = _adapter()

    assert adapter._voice_auto_join_routes == ()


def test_enabled_config_parses_explicit_route_options():
    adapter = _adapter(
        _route_config(
            leave_when_no_trigger_users=False,
            leave_grace_seconds=12.5,
        )
    )

    assert adapter._voice_auto_join_reconnect is True
    assert len(adapter._voice_auto_join_routes) == 1
    route = adapter._voice_auto_join_routes[0]
    assert route.guild_id == 100
    assert route.voice_channel_id == 200
    assert route.text_channel_id == 300
    assert route.trigger_user_ids == frozenset({42})
    assert route.leave_when_no_trigger_users is False
    assert route.leave_grace_seconds == 12.5


def test_invalid_route_fails_closed():
    adapter = _adapter(
        {
            "voice_auto_join": {
                "enabled": True,
                "routes": [
                    {
                        "guild_id": "100",
                        "voice_channel_id": "200",
                        "text_channel_id": "300",
                        "trigger_user_ids": [],
                    }
                ],
            }
        }
    )

    assert adapter._voice_auto_join_routes == ()


def test_duplicate_routes_for_one_guild_fail_closed_to_first_route():
    config = _route_config()
    config["voice_auto_join"]["routes"].append(
        {
            "guild_id": "100",
            "voice_channel_id": "201",
            "text_channel_id": "301",
            "trigger_user_ids": ["43"],
        }
    )

    adapter = _adapter(config)

    assert [route.voice_channel_id for route in adapter._voice_auto_join_routes] == [200]


@pytest.mark.asyncio
async def test_wrong_channel_and_unauthorized_user_do_not_join():
    adapter = _adapter(_route_config())
    wrong_channel = _channel(channel_id=201)
    right_channel = _channel(channel_id=200)

    wrong_channel_member = _member(channel=wrong_channel)
    unauthorized_member = _member(user_id=43, channel=right_channel)

    await adapter._handle_voice_state_update(
        wrong_channel_member, _state(None), _state(wrong_channel)
    )
    await adapter._handle_voice_state_update(
        unauthorized_member, _state(None), _state(right_channel)
    )

    adapter._voice_auto_join_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_user_joins_exactly_once_on_duplicate_events():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]

    await adapter._handle_voice_state_update(member, _state(None), _state(channel))
    voice_client = MagicMock()
    voice_client.is_connected.return_value = True
    voice_client.channel = channel
    adapter._voice_clients[100] = voice_client
    await adapter._handle_voice_state_update(member, _state(None), _state(channel))

    adapter._voice_auto_join_callback.assert_awaited_once()
    action, route, actual_channel, actual_member = (
        adapter._voice_auto_join_callback.await_args.args
    )
    assert action == "join"
    assert route.guild_id == 100
    assert actual_channel is channel
    assert actual_member is member


@pytest.mark.asyncio
async def test_concurrent_duplicate_join_events_start_one_session():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]

    async def delayed_join(*_args):
        await asyncio.sleep(0)
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.channel = channel
        adapter._voice_clients[100] = voice_client
        return True

    adapter._voice_auto_join_callback = AsyncMock(side_effect=delayed_join)
    await asyncio.gather(
        adapter._handle_voice_state_update(member, _state(None), _state(channel)),
        adapter._handle_voice_state_update(member, _state(None), _state(channel)),
    )

    adapter._voice_auto_join_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_error_is_contained_and_can_retry(caplog):
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]
    adapter._voice_auto_join_callback = AsyncMock(side_effect=RuntimeError("boom"))

    await adapter._handle_voice_state_update(member, _state(None), _state(channel))

    assert adapter._voice_auto_join_active == {}
    assert "100" not in caplog.text
    assert "200" not in caplog.text
    assert "42" not in caplog.text
    adapter._voice_auto_join_callback = AsyncMock(return_value=True)
    await adapter._handle_voice_state_update(member, _state(None), _state(channel))
    adapter._voice_auto_join_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_join_does_not_take_over_a_manual_voice_connection():
    adapter = _adapter(_route_config())
    voice_client = MagicMock()
    voice_client.is_connected.return_value = True
    adapter._voice_clients[100] = voice_client
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]

    await adapter._handle_voice_state_update(member, _state(None), _state(channel))

    adapter._voice_auto_join_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_atomic_auto_join_guard_does_not_claim_manual_connection():
    adapter = _adapter(_route_config())
    existing = MagicMock()
    existing.is_connected.return_value = True
    existing.channel = SimpleNamespace(id=201)
    existing.move_to = AsyncMock()
    adapter._voice_clients[100] = existing

    joined = await adapter.join_voice_channel(
        _channel(channel_id=200), move_existing=False
    )

    assert joined is False
    existing.move_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_join_claim_wins_over_in_flight_auto_join():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def delayed_join(*_args):
        callback_started.set()
        await release_callback.wait()
        return True

    adapter._voice_auto_join_callback = AsyncMock(side_effect=delayed_join)
    auto_join = asyncio.create_task(
        adapter._handle_voice_state_update(member, _state(None), _state(channel))
    )
    await callback_started.wait()

    adapter._begin_manual_voice_join(100)
    release_callback.set()
    await auto_join
    adapter._end_manual_voice_join(100)

    assert adapter._voice_auto_join_active == {}


@pytest.mark.asyncio
async def test_reentry_waits_for_committed_auto_leave_then_reconnects():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]
    leave_started = asyncio.Event()
    finish_leave = asyncio.Event()
    join_count = 0

    async def lifecycle(action, _route, actual_channel, _member):
        nonlocal join_count
        if action == "join":
            join_count += 1
            voice_client = MagicMock()
            voice_client.is_connected.return_value = True
            voice_client.channel = actual_channel
            adapter._voice_clients[100] = voice_client
            return True
        adapter._voice_clients.pop(100, None)
        leave_started.set()
        await finish_leave.wait()
        return True

    adapter._voice_auto_join_callback = AsyncMock(side_effect=lifecycle)
    await adapter._handle_voice_state_update(member, _state(None), _state(channel))

    channel.members.clear()
    await adapter._handle_voice_state_update(member, _state(channel), _state(None))
    await leave_started.wait()

    channel.members[:] = [member]
    reentry = asyncio.create_task(
        adapter._handle_voice_state_update(member, _state(None), _state(channel))
    )
    await asyncio.sleep(0)
    assert reentry.done() is False

    finish_leave.set()
    await reentry

    assert join_count == 2
    assert adapter._voice_auto_join_active == {100: 200}
    assert adapter._voice_clients[100].is_connected()


@pytest.mark.asyncio
async def test_inactivity_timeout_keeps_present_auto_join_trigger_connected(monkeypatch):
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]
    voice_client = MagicMock()
    voice_client.channel = channel
    adapter._voice_clients[100] = voice_client
    adapter._voice_auto_join_active[100] = 200
    adapter._voice_text_channels[100] = 300
    monkeypatch.setattr(adapter, "VOICE_TIMEOUT", 0)
    adapter.leave_voice_channel = AsyncMock()

    await adapter._voice_timeout_handler(100)

    adapter.leave_voice_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_trigger_user_leaving_disconnects_after_grace_period():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]

    await adapter._handle_voice_state_update(member, _state(None), _state(channel))
    channel.members.clear()
    await adapter._handle_voice_state_update(member, _state(channel), _state(None))
    await asyncio_sleep_until_tasks_run()

    assert [call.args[0] for call in adapter._voice_auto_join_callback.await_args_list] == [
        "join",
        "leave",
    ]


@pytest.mark.asyncio
async def test_other_trigger_user_keeps_auto_join_session_connected():
    config = _route_config(trigger_user_ids=["42", "43"])
    adapter = _adapter(config)
    channel = _channel()
    leaving = _member(user_id=42, channel=channel)
    staying = _member(user_id=43, channel=channel)
    channel.members[:] = [leaving, staying]

    await adapter._handle_voice_state_update(leaving, _state(None), _state(channel))
    channel.members[:] = [staying]
    await adapter._handle_voice_state_update(leaving, _state(channel), _state(None))
    await asyncio_sleep_until_tasks_run()

    assert [call.args[0] for call in adapter._voice_auto_join_callback.await_args_list] == [
        "join"
    ]


@pytest.mark.asyncio
async def test_startup_reconciliation_joins_when_trigger_is_already_present():
    adapter = _adapter(_route_config())
    channel = _channel()
    member = _member(channel=channel)
    channel.members[:] = [member]
    guild = SimpleNamespace(id=100, get_channel=lambda channel_id: channel if channel_id == 200 else None)
    client = MagicMock()
    adapter._client = client
    client.get_guild.return_value = guild

    await adapter.reconcile_voice_auto_join()

    adapter._voice_auto_join_callback.assert_awaited_once_with(
        "join", adapter._voice_auto_join_routes[0], channel, member
    )


@pytest.mark.asyncio
async def test_reconciliation_tries_later_trigger_when_first_is_rejected():
    config = _route_config(trigger_user_ids=["42", "43"])
    adapter = _adapter(config)
    channel = _channel()
    first = _member(user_id=42, channel=channel)
    second = _member(user_id=43, channel=channel)
    channel.members[:] = [first, second]
    guild = SimpleNamespace(
        id=100,
        get_channel=lambda channel_id: channel if channel_id == 200 else None,
    )
    client = MagicMock()
    adapter._client = client
    client.get_guild.return_value = guild
    adapter._voice_auto_join_callback = AsyncMock(side_effect=[False, True])

    await adapter.reconcile_voice_auto_join()

    assert adapter._voice_auto_join_callback.await_count == 2
    assert adapter._voice_auto_join_active == {100: 200}


@pytest.mark.asyncio
async def test_startup_reconciliation_can_be_disabled_per_config():
    config = _route_config()
    config["voice_auto_join"]["reconnect_on_startup"] = False
    adapter = _adapter(config)

    await adapter.reconcile_voice_auto_join()

    adapter._voice_auto_join_callback.assert_not_awaited()


def test_yaml_bridge_preserves_voice_auto_join_as_adapter_config():
    voice_auto_join = _route_config()["voice_auto_join"]

    seeded = _apply_yaml_config({}, {"voice_auto_join": voice_auto_join})

    assert seeded is not None
    assert seeded["voice_auto_join"] == voice_auto_join
    assert seeded["voice_auto_join"] is not voice_auto_join


async def asyncio_sleep_until_tasks_run():
    """Allow zero-grace leave tasks to run without timing-sensitive sleeps."""
    for _ in range(3):
        await asyncio.sleep(0)
