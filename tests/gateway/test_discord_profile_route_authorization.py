"""Security contracts for Discord route-scoped profile authorization."""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from gateway.profile_routing import ProfileRoute, ProfileRouteAuthorization
from plugins.platforms.discord.adapter import DiscordAdapter


class _Thread:
    def __init__(self, thread_id: int, parent_id: int, guild):
        self.id = thread_id
        self.parent_id = parent_id
        self.guild = guild


def _adapter(monkeypatch, *, global_users=("1",), routes=()):
    # Some availability tests intentionally reload the adapter with Discord
    # absent. Rebind the defining module globals for the already-imported class.
    monkeypatch.setitem(
        DiscordAdapter._matched_profile_route.__globals__, "discord", discord
    )
    monkeypatch.setattr(discord, "Thread", _Thread)
    adapter = object.__new__(DiscordAdapter)
    adapter._allowed_user_ids = set(global_users)
    adapter._allowed_role_ids = set()
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._dedup = SimpleNamespace(
        is_duplicate=lambda _message_id: False,
        contains=lambda _message_id: False,
    )
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True, profile_routes=list(routes))
    )
    adapter._self_is_explicitly_mentioned = lambda _message: False
    adapter._self_is_raw_mentioned = lambda _message: False
    adapter._get_allow_bots = lambda: "none"
    return adapter


def _route(*, users=(), roles=(555,), enabled=True):
    return ProfileRoute(
        name="event-pet-forum",
        platform="discord",
        profile="event-pet-helper",
        guild_id="111",
        chat_id="222",
        enabled=enabled,
        authorization=ProfileRouteAuthorization(
            allowed_users=tuple(users),
            allowed_roles=tuple(roles),
        ),
    )


def _message(*, user_id=42, roles=(555,), content="revise the alias"):
    guild = SimpleNamespace(id=111)
    author = SimpleNamespace(
        id=user_id,
        bot=False,
        guild=guild,
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
    )
    guild.get_member = lambda candidate: author if candidate == user_id else None
    return SimpleNamespace(
        id=1234,
        author=author,
        guild=guild,
        channel=_Thread(333, 222, guild),
        type=discord.MessageType.default,
        content=content,
        mentions=[],
    )


def test_matching_forum_route_admits_role_without_global_grant(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])

    assert adapter._discord_message_admission(_message(), claim=True) == (
        True,
        False,
        True,
    )


def test_same_role_outside_route_does_not_widen_global_access(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    message = _message()
    message.channel = _Thread(333, 999, message.guild)

    assert adapter._discord_message_admission(message, claim=True) == (
        False,
        False,
        False,
    )


def test_route_policy_replaces_global_allowlist_inside_route(monkeypatch):
    adapter = _adapter(
        monkeypatch, global_users=("42",), routes=[_route(users=(), roles=())]
    )

    assert adapter._discord_message_admission(_message(), claim=True) == (
        False,
        False,
        False,
    )


def test_route_without_authorization_keeps_global_policy(monkeypatch):
    route = ProfileRoute(
        name="legacy",
        platform="discord",
        profile="limited",
        guild_id="111",
        chat_id="222",
    )
    adapter = _adapter(monkeypatch, global_users=("42",), routes=[route])

    assert adapter._discord_message_admission(_message(), claim=True) == (
        True,
        False,
        False,
    )


def test_route_user_id_grant_is_scoped_to_matching_forum(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route(users=("42",), roles=())])

    assert adapter._discord_message_admission(_message(roles=()), claim=True) == (
        True,
        False,
        True,
    )


def test_route_role_from_different_guild_is_rejected(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    message = _message()
    foreign_guild = SimpleNamespace(id=999)
    message.author.guild = foreign_guild
    message.guild.get_member = lambda _candidate: None

    assert adapter._discord_message_admission(message, claim=True) == (
        False,
        False,
        False,
    )


def test_route_role_member_cache_is_scoped_to_origin_guild(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    message = _message()
    message.author.guild = None
    message.guild.get_member = lambda _candidate: SimpleNamespace(
        guild=message.guild,
        roles=[SimpleNamespace(id=555)],
    )

    assert adapter._discord_message_admission(message, claim=True) == (
        True,
        False,
        True,
    )


def test_route_role_foreign_cached_member_is_rejected(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    message = _message()
    message.author.guild = None
    message.guild.get_member = lambda _candidate: SimpleNamespace(
        guild=SimpleNamespace(id=999),
        roles=[SimpleNamespace(id=555)],
    )

    assert adapter._discord_message_admission(message, claim=True) == (
        False,
        False,
        False,
    )


def test_route_grant_cannot_invoke_text_gateway_command(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])

    assert adapter._discord_message_admission(
        _message(content="<@999> /restart"), claim=True
    ) == (False, False, False)


def test_global_user_retains_gateway_commands_when_route_also_allows_them(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        global_users=("42",),
        routes=[_route(users=("42",), roles=())],
    )

    assert adapter._discord_message_admission(
        _message(roles=(), content="<@999> /restart"), claim=True
    ) == (True, False, False)


def test_route_roles_enable_server_members_intent(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])

    assert adapter._profile_route_role_ids() == {555}


def test_disabled_route_does_not_enable_server_members_intent(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route(enabled=False)])

    assert adapter._profile_route_role_ids() == set()


def test_route_role_is_named_in_privileged_intent_diagnostics(monkeypatch):
    class PrivilegedIntentsRequired(Exception):
        pass

    adapter = _adapter(monkeypatch, routes=[_route()])
    code, guidance, retryable = adapter._classify_connect_exception(
        PrivilegedIntentsRequired()
    )

    assert code == "discord_intents_required"
    assert "Server Members Intent" in guidance
    assert retryable is False


def test_transport_bot_allowance_cannot_bypass_route_policy(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    adapter._get_allow_bots = lambda: "all"
    message = _message()
    message.author.bot = True

    assert adapter._discord_message_admission(message, claim=True) == (
        False,
        False,
        False,
    )


def test_authorization_is_inert_when_multiplexing_is_off(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    adapter.gateway_runner.config.multiplex_profiles = False

    assert adapter._discord_message_admission(_message(), claim=True) == (
        False,
        False,
        False,
    )


@pytest.mark.asyncio
async def test_dispatch_preserves_route_authorization_provenance(monkeypatch):
    adapter = _adapter(monkeypatch, routes=[_route()])
    adapter._ready_event = asyncio.Event()
    adapter._ready_event.set()
    adapter._handle_message = AsyncMock(return_value=True)
    message = _message()

    assert await adapter._dispatch_discord_message(message) is True
    adapter._handle_message.assert_awaited_once_with(
        message,
        role_authorized=False,
        route_authorized=True,
    )
