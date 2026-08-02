"""Safety contracts for Discord auto-thread routing and REST retries."""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import DiscordAdapter


class _FakeResponse:
    def __init__(self, status: int, *, retry_after: str | None = None):
        self.status = status
        self.reason = "test response"
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after


class _TestHTTPException(Exception):
    def __init__(self, response: _FakeResponse, message: str):
        super().__init__(message)
        self.response = response
        self.status = response.status


class _TestRateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"Retry in {retry_after} seconds")
        self.retry_after = retry_after


class _FakeTextChannel:
    def __init__(self, channel_id: int, guild=None):
        self.id = channel_id
        self.name = "general"
        self.guild = guild
        self.topic = None
        self.send = AsyncMock()

    def history(self, *args, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover - make this an async generator

        return _empty()


class _FakeThread(discord.Thread):
    """Minimal real ``discord.Thread`` subtype for routing checks."""

    def __init__(
        self,
        channel_id: int,
        parent_type: discord.ChannelType,
        *,
        parent_id: int = 100,
        guild_id: int = 1,
    ):
        parent = SimpleNamespace(
            id=parent_id,
            name="parent",
            topic="Parent topic",
            type=parent_type,
        )
        guild = SimpleNamespace(
            id=guild_id,
            name="TestGuild",
            get_channel=lambda channel_id: parent if channel_id == parent.id else None,
        )
        self.id = channel_id
        self.name = "existing-post"
        self.guild = guild
        self.parent_id = parent.id
        self.send = AsyncMock()
        self.fetch_message = AsyncMock()

    def history(self, *args, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover - make this an async generator

        return _empty()


class _FakeForumChannel(discord.ForumChannel):
    """Minimal real ``discord.ForumChannel`` subtype for routing checks."""

    def __init__(self, channel_id: int, guild=None):
        self.id = channel_id
        self.name = "ideas"
        self.guild = guild or SimpleNamespace(id=1, name="TestGuild")
        self.type = discord.ChannelType.forum
        self.create_thread = AsyncMock()


def _message(channel, *, guild=None):
    return SimpleNamespace(
        id=12345,
        author=SimpleNamespace(
            id=42,
            display_name="Jezza",
            name="Jezza",
            bot=False,
        ),
        content="Hello from a projected channel",
        channel=channel,
        guild=guild,
        attachments=[],
        mentions=[],
        reference=None,
        created_at=None,
        type=discord.MessageType.default,
    )


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(
        discord_platform.discord,
        "HTTPException",
        _TestHTTPException,
        raising=False,
    )
    monkeypatch.setattr(
        discord_platform.discord,
        "RateLimited",
        _TestRateLimited,
        raising=False,
    )
    instance = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    instance._client = SimpleNamespace(
        user=SimpleNamespace(id=99999, name="HermesBot"),
        get_channel=MagicMock(return_value=None),
        fetch_channel=AsyncMock(),
    )
    instance._ready_event.set()
    instance._text_batch_delay_seconds = 0
    instance.handle_message = AsyncMock()
    return instance


def _http_error(status: int, *, retry_after: str | None = None) -> _TestHTTPException:
    return _TestHTTPException(
        _FakeResponse(status, retry_after=retry_after),
        "test failure",
    )


def _message_thread_source():
    guild = SimpleNamespace(id=1, name="TestGuild")
    channel = _FakeTextChannel(200, guild=guild)
    message = _message(channel, guild=guild)
    thread = _FakeThread(
        message.id,
        discord.ChannelType.text,
        parent_id=channel.id,
        guild_id=guild.id,
    )
    return channel, message, thread


@pytest.mark.asyncio
async def test_missing_guild_context_is_resolved_before_auto_thread(
    adapter, monkeypatch
):
    """REST context repairs the discord.py ValueError precondition."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    projected_channel = _FakeTextChannel(200, guild=None)
    authoritative_guild = SimpleNamespace(id=1, name="TestGuild")
    authoritative_channel = _FakeTextChannel(200, guild=authoritative_guild)
    adapter._client.fetch_channel.return_value = authoritative_channel

    message = _message(projected_channel, guild=None)
    thread = _FakeThread(
        message.id,
        discord.ChannelType.text,
        parent_id=authoritative_channel.id,
        guild_id=authoritative_guild.id,
    )

    async def create_thread(**kwargs):
        if message.guild is None:
            raise ValueError("This message does not have guild info attached.")
        return thread

    message.create_thread = AsyncMock(side_effect=create_thread)

    dispatched = await adapter._dispatch_discord_message(message)

    assert dispatched is True
    adapter._client.fetch_channel.assert_awaited_once_with(200)
    assert message.channel is authoritative_channel
    assert message.guild is authoritative_guild
    message.create_thread.assert_awaited_once()
    projected_channel.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_type",
    [
        discord.ChannelType.text,
        discord.ChannelType.forum,
        discord.ChannelType.media,
    ],
    ids=["text-thread", "forum-post", "media-post"],
)
async def test_authoritative_thread_context_never_creates_nested_thread(
    adapter,
    monkeypatch,
    parent_type,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_thread = _FakeThread(200, parent_type)
    adapter._client.fetch_channel.return_value = authoritative_thread
    adapter._auto_create_thread = AsyncMock()

    dispatched = await adapter._dispatch_discord_message(
        _message(projected_channel, guild=projected_channel.guild)
    )

    assert dispatched is True
    adapter._auto_create_thread.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.chat_id == "200"
    assert event.source.parent_chat_id == "100"


@pytest.mark.asyncio
async def test_recovered_projected_thread_resolves_before_thread_mention_gate(
    adapter,
    monkeypatch,
):
    """Reconnect recovery must apply thread gates to the REST-resolved type."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_thread = _FakeThread(200, discord.ChannelType.forum)
    adapter._client.fetch_channel.return_value = authoritative_thread
    adapter._auto_create_thread = AsyncMock()
    adapter._threads.mark("200")
    claim = MagicMock(side_effect=adapter._dedup.is_duplicate)
    adapter._dedup.is_duplicate = claim

    dispatched = await adapter._dispatch_recovered_message(
        _message(projected_channel, guild=projected_channel.guild)
    )

    assert dispatched is True
    adapter._client.fetch_channel.assert_awaited_once_with(200)
    adapter._auto_create_thread.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.chat_id == "200"
    claim.assert_called_once_with("12345")


@pytest.mark.asyncio
async def test_channel_update_invalidates_cached_parent_and_child(adapter):
    guild = SimpleNamespace(id=1, name="TestGuild")
    cached_parent = _FakeForumChannel(100, guild=guild)
    cached_thread = _FakeThread(
        200,
        discord.ChannelType.forum,
        parent_id=cached_parent.id,
        guild_id=guild.id,
    )
    adapter._cache_authoritative_message_channel(cached_parent)
    adapter._cache_authoritative_message_channel(cached_thread)

    # Mirrors the on_guild_channel_update/delete handlers: the provider
    # event invalidates both the changed parent and cached child context.
    adapter._invalidate_authoritative_channel(100, include_children=True)

    assert 100 not in adapter._authoritative_message_channels
    assert 200 not in adapter._authoritative_message_channels
    refreshed_thread = _FakeThread(
        200,
        discord.ChannelType.forum,
        parent_id=300,
        guild_id=guild.id,
    )
    adapter._client.fetch_channel.return_value = refreshed_thread
    resolved, authoritative = await adapter._resolve_authoritative_channel(
        200,
        _FakeTextChannel(200, guild=guild),
        operation_name="post-update channel resolution",
    )

    assert authoritative is True
    assert resolved is refreshed_thread
    adapter._client.fetch_channel.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_thread_parent_update_refetches_and_applies_new_channel_policy(
    adapter,
    monkeypatch,
):
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "100")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    old_thread = _FakeThread(
        200,
        discord.ChannelType.forum,
        parent_id=100,
    )
    adapter._client.fetch_channel.return_value = old_thread
    first = _message(_FakeTextChannel(200, guild=None), guild=None)

    assert await adapter._dispatch_discord_message(first) is True

    # Mirrors on_raw_thread_update. The next event must use a
    # new authoritative object rather than authorizing against parent 100.
    adapter._invalidate_authoritative_channel(200)
    new_thread = _FakeThread(
        200,
        discord.ChannelType.forum,
        parent_id=300,
    )
    adapter._client.fetch_channel.return_value = new_thread
    second = _message(_FakeTextChannel(200, guild=None), guild=None)
    second.id = 12346

    assert await adapter._dispatch_discord_message(second) is False
    assert adapter._client.fetch_channel.await_count == 2
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_text_send_routes_authoritative_forum_and_reuses_cache(adapter):
    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_forum = _FakeForumChannel(200, guild=projected_channel.guild)
    adapter._client.get_channel.return_value = projected_channel
    adapter._client.fetch_channel.return_value = authoritative_forum
    adapter._send_to_forum = AsyncMock(
        return_value=SendResult(success=True, message_id="777")
    )

    first = await adapter.send("200", "first")
    second = await adapter.send("200", "second")

    assert first.success is True
    assert second.success is True
    assert adapter._send_to_forum.await_count == 2
    projected_channel.send.assert_not_awaited()
    adapter._client.fetch_channel.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_stale_text_send_routes_authoritative_thread(adapter):
    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_thread = _FakeThread(200, discord.ChannelType.forum)
    authoritative_thread.send.return_value = SimpleNamespace(id=777)
    adapter._client.get_channel.return_value = projected_channel
    adapter._client.fetch_channel.return_value = authoritative_thread

    result = await adapter.send("200", "reply in the existing post")

    assert result.success is True
    authoritative_thread.send.assert_awaited_once()
    projected_channel.send.assert_not_awaited()
    adapter._client.fetch_channel.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_definitive_thread_id_send_does_not_add_rest_lookup(adapter):
    thread = _FakeThread(200, discord.ChannelType.text)
    thread.send.return_value = SimpleNamespace(id=777)
    adapter._client.get_channel.return_value = thread

    first = await adapter.send("200", "first")
    second = await adapter.send("200", "second")
    thread.send.side_effect = RuntimeError("RAW_SEND_SENTINEL")
    failed = await adapter.send("200", "third")

    assert first.success is True
    assert second.success is True
    assert failed.success is False
    assert "RAW_SEND_SENTINEL" not in (failed.error or "")
    assert thread.send.await_count == 3
    adapter._client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["self", "bot", "type"])
async def test_provider_preflight_rejections_do_not_fetch(adapter, rejection):
    message = _message(_FakeTextChannel(200, guild=None), guild=None)
    if rejection == "self":
        message.author = adapter._client.user
    elif rejection == "bot":
        message.author.bot = True
    else:
        message.type = object()

    dispatched = await adapter._dispatch_discord_message(message)

    assert dispatched is False
    adapter._client.fetch_channel.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_guild_context_applies_guild_role_policy(adapter, monkeypatch):
    """A partial guild=None event must not be evaluated under DM role policy."""
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    adapter._allowed_role_ids = {7}

    projected_channel = _FakeTextChannel(200, guild=None)
    authoritative_guild = SimpleNamespace(
        id=1,
        name="TestGuild",
        get_member=lambda _user_id: None,
    )
    authoritative_channel = _FakeTextChannel(200, guild=authoritative_guild)
    adapter._client.fetch_channel.return_value = authoritative_channel
    message = _message(projected_channel, guild=None)
    message.author.roles = [SimpleNamespace(id=7)]

    dispatched = await adapter._dispatch_discord_message(message)

    assert dispatched is True
    assert message.guild is authoritative_guild
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolved_thread_parent_is_used_by_channel_authorization(
    adapter,
    monkeypatch,
):
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "100")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    projected_channel = _FakeTextChannel(200, guild=None)
    authoritative_thread = _FakeThread(
        200,
        discord.ChannelType.forum,
        parent_id=100,
    )
    adapter._client.fetch_channel.return_value = authoritative_thread

    dispatched = await adapter._dispatch_discord_message(
        _message(projected_channel, guild=None)
    )

    assert dispatched is True
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == "200"
    assert event.source.parent_chat_id == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_fetches", "claim_retained"),
    [
        (_http_error(403), 1, True),
        (_http_error(404), 1, True),
        (_http_error(429, retry_after="0"), 2, False),
        (_http_error(500), 2, False),
        (TimeoutError("temporary network failure"), 2, False),
    ],
    ids=["forbidden", "not-found", "rate-limit", "server-error", "network"],
)
async def test_unresolved_ambiguous_context_fails_before_authorization(
    adapter,
    monkeypatch,
    error,
    expected_fetches,
    claim_retained,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    monkeypatch.setattr(discord_platform.asyncio, "sleep", AsyncMock())
    adapter._client.fetch_channel.side_effect = error
    adapter._discord_message_context_admission = MagicMock()
    projected_channel = _FakeTextChannel(200, guild=None)
    message = _message(projected_channel, guild=None)
    message.create_thread = AsyncMock()

    dispatched = await adapter._dispatch_discord_message(message)

    assert dispatched is False
    assert adapter._client.fetch_channel.await_count == expected_fetches
    assert all(call.args == (200,) for call in adapter._client.fetch_channel.await_args_list)
    assert adapter._dedup.contains(str(message.id)) is claim_retained
    adapter._discord_message_context_admission.assert_not_called()
    message.create_thread.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    projected_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_resolution_claim_blocks_race_then_allows_recovery(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.setattr(discord_platform.asyncio, "sleep", AsyncMock())
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    authoritative_channel = _FakeTextChannel(
        200,
        guild=SimpleNamespace(id=1, name="TestGuild"),
    )
    fetch_count = 0

    async def fetch_channel(_channel_id):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            fetch_started.set()
            await release_fetch.wait()
        if fetch_count <= 2:
            raise TimeoutError("temporary network failure")
        return authoritative_channel

    adapter._client.fetch_channel.side_effect = fetch_channel
    projected_channel = _FakeTextChannel(200, guild=None)
    first = asyncio.create_task(
        adapter._dispatch_discord_message(_message(projected_channel, guild=None))
    )
    await fetch_started.wait()

    racing_recovery = await adapter._dispatch_recovered_message(
        _message(projected_channel, guild=None)
    )
    assert racing_recovery is False
    assert fetch_count == 1

    release_fetch.set()
    assert await first is False
    assert adapter._dedup.contains("12345") is False

    recovered = await adapter._dispatch_recovered_message(
        _message(projected_channel, guild=None)
    )
    assert recovered is True
    assert fetch_count == 3
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_type_value_is_not_trusted_without_authoritative_read(adapter):
    partial = SimpleNamespace(
        id=200,
        type=discord.ChannelType.forum,
        guild=SimpleNamespace(id=1),
    )
    adapter._client.fetch_channel.side_effect = _http_error(403)

    resolved, authoritative = await adapter._resolve_authoritative_channel(
        partial.id,
        partial,
        operation_name="partial forum resolution",
    )

    assert resolved is partial
    assert authoritative is False
    adapter._client.fetch_channel.assert_awaited_once_with(partial.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("This message does not have guild info attached."),
        _http_error(400),
        _http_error(403),
        _http_error(404),
        RuntimeError("unexpected adapter failure"),
    ],
    ids=["missing-guild", "bad-request", "forbidden", "not-found", "unknown"],
)
async def test_permanent_auto_thread_failures_are_not_retried_or_seeded(
    adapter,
    monkeypatch,
    error,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel, message, _thread = _message_thread_source()
    message.create_thread = AsyncMock(side_effect=error)

    result = await adapter._auto_create_thread(message)

    assert result is None
    message.create_thread.assert_awaited_once()
    adapter._client.fetch_channel.assert_not_awaited()
    sleep.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_timeout_reconciles_by_source_message_id(adapter, monkeypatch):
    """A lost create response is accepted by authoritative known-ID read-back."""
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel, message, thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=TimeoutError("RAW_TIMEOUT_SENTINEL")
    )
    adapter._client.fetch_channel.return_value = thread

    result = await adapter._auto_create_thread(message)

    assert result is thread
    message.create_thread.assert_awaited_once()
    adapter._client.fetch_channel.assert_awaited_once_with(message.id)
    assert adapter._authoritative_message_channels[message.id] is thread
    sleep.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_readback_404_allows_one_replay(adapter, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    _channel, message, thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=[TimeoutError("RAW_TIMEOUT_SENTINEL"), thread]
    )
    adapter._client.fetch_channel.side_effect = _http_error(404)

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    adapter._client.fetch_channel.assert_awaited_once_with(message.id)
    sleep.assert_awaited_once_with(0.75)


@pytest.mark.asyncio
async def test_replay_400_reconciles_committed_thread(adapter, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    _channel, message, thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=[
            aiohttp.ClientConnectionError("RAW_NETWORK_SENTINEL"),
            _http_error(400),
        ]
    )
    adapter._client.fetch_channel.side_effect = [
        _http_error(404),
        thread,
    ]

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    assert adapter._client.fetch_channel.await_count == 2
    sleep.assert_awaited_once_with(0.75)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readback",
    [
        _FakeTextChannel(12345, guild=SimpleNamespace(id=1)),
        _FakeThread(
            12345,
            discord.ChannelType.text,
            parent_id=999,
            guild_id=1,
        ),
    ],
    ids=["wrong-type", "wrong-parent"],
)
async def test_unexpected_readback_fails_closed_without_replay(
    adapter,
    monkeypatch,
    readback,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    _channel, message, _thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=TimeoutError("RAW_TIMEOUT_SENTINEL")
    )
    adapter._client.fetch_channel.return_value = readback

    result = await adapter._auto_create_thread(message)

    assert result is None
    message.create_thread.assert_awaited_once()
    adapter._client.fetch_channel.assert_awaited_once_with(message.id)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_delay"),
    [
        (_http_error(429, retry_after="2.5"), 2.5),
        (_TestRateLimited(retry_after=4.0), 4.0),
    ],
    ids=["http-429", "discord-rate-limited"],
)
async def test_rate_limit_replay_honors_retry_after(
    adapter,
    monkeypatch,
    error,
    expected_delay,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    _channel, message, thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=[error, thread]
    )

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    adapter._client.fetch_channel.assert_not_awaited()
    sleep.assert_awaited_once_with(expected_delay)


@pytest.mark.asyncio
async def test_replay_failure_final_404_proves_absence(adapter, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    _channel, message, _thread = _message_thread_source()
    message.create_thread = AsyncMock(
        side_effect=[
            TimeoutError("RAW_FIRST_SENTINEL"),
            _http_error(400),
        ]
    )
    adapter._client.fetch_channel.side_effect = [
        _http_error(404),
        _http_error(404),
    ]

    result = await adapter._auto_create_thread(message)

    assert result is None
    assert message.create_thread.await_count == 2
    assert adapter._client.fetch_channel.await_count == 2
    sleep.assert_awaited_once_with(0.75)


def _owned_seed(parent, *, seed_id=777, create_result=None, create_error=None):
    create_thread = AsyncMock(return_value=create_result)
    if create_error is not None:
        create_thread.side_effect = create_error
    return SimpleNamespace(
        id=seed_id,
        channel=parent,
        guild=parent.guild,
        create_thread=create_thread,
        delete=AsyncMock(),
    )


def _interaction(channel):
    return SimpleNamespace(
        channel=channel,
        channel_id=channel.id,
        guild_id=getattr(getattr(channel, "guild", None), "id", None),
        user=SimpleNamespace(display_name="Jezza"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "seed_content"),
    [
        ("slash", "\U0001f9f5 Thread requested via Hermes: **Planning**"),
        ("handoff", "\U0001f9f5 Hermes handoff: **Planning**"),
    ],
)
async def test_text_parent_uses_one_message_path_and_cleans_permanent_orphan(
    adapter,
    path,
    seed_content,
):
    parent = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    parent.create_thread = AsyncMock()
    seed = _owned_seed(
        parent,
        seed_id=777,
        create_error=ValueError("RAW_THREAD_SENTINEL"),
    )
    parent.send.return_value = seed
    adapter._client.fetch_channel.return_value = parent

    if path == "slash":
        result = await adapter._create_thread(_interaction(parent), name="Planning")
        assert "error" in result
        assert "RAW_THREAD_SENTINEL" not in result["error"]
    else:
        result = await adapter.create_handoff_thread(str(parent.id), "Planning")
        assert result is None

    parent.send.assert_awaited_once_with(seed_content)
    parent.create_thread.assert_not_awaited()
    seed.create_thread.assert_awaited_once()
    seed.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_slash_stale_text_resolves_authoritative_forum(adapter):
    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_forum = _FakeForumChannel(200, guild=projected_channel.guild)
    thread = _FakeThread(
        888,
        discord.ChannelType.forum,
        parent_id=authoritative_forum.id,
        guild_id=authoritative_forum.guild.id,
    )
    authoritative_forum.create_thread.return_value = thread
    adapter._client.fetch_channel.return_value = authoritative_forum

    result = await adapter._create_thread(
        _interaction(projected_channel),
        name="Planning",
    )

    assert result == {
        "success": True,
        "thread_id": str(thread.id),
        "thread_name": thread.name,
    }
    authoritative_forum.create_thread.assert_awaited_once()
    projected_channel.send.assert_not_awaited()
    adapter._client.fetch_channel.assert_awaited_once_with(projected_channel.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_projection", [False, True], ids=["thread", "stale-text"])
async def test_slash_rejects_nested_thread(adapter, stale_projection):
    thread = _FakeThread(200, discord.ChannelType.text)
    channel = thread
    if stale_projection:
        channel = _FakeTextChannel(200, guild=thread.guild)
        adapter._client.fetch_channel.return_value = thread

    result = await adapter._create_thread(_interaction(channel), name="Nested")

    assert "nested thread" in result["error"].lower()
    thread.send.assert_not_awaited()
    if stale_projection:
        channel.send.assert_not_awaited()
        adapter._client.fetch_channel.assert_awaited_once_with(channel.id)
    else:
        adapter._client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_exhausted_429_is_sanitized_and_cleans_owned_seed(
    adapter,
    monkeypatch,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    parent = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    seed = _owned_seed(parent)
    seed.create_thread.side_effect = [
        _TestHTTPException(
            _FakeResponse(429, retry_after="1.25"),
            "RAW_FIRST_SLASH_429_SENTINEL",
        ),
        _TestHTTPException(
            _FakeResponse(429, retry_after="3.5"),
            "RAW_SECOND_SLASH_429_SENTINEL",
        ),
    ]
    parent.send.return_value = seed
    adapter._client.fetch_channel.return_value = parent

    result = await adapter._create_thread(_interaction(parent), name="Planning")

    assert "remained rate limited after one retry" in result["error"]
    assert "3.5 seconds" in result["error"]
    assert "RAW_FIRST_SLASH_429_SENTINEL" not in result["error"]
    assert "RAW_SECOND_SLASH_429_SENTINEL" not in result["error"]
    assert seed.create_thread.await_count == 2
    seed.delete.assert_awaited_once()
    adapter._client.fetch_channel.assert_awaited_once_with(parent.id)
    sleep.assert_awaited_once_with(1.25)


@pytest.mark.asyncio
async def test_slash_keeps_owned_seed_when_ambiguous_outcome_is_not_absent(adapter):
    parent = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    parent.create_thread = AsyncMock()
    seed = _owned_seed(
        parent,
        create_error=TimeoutError("RAW_TIMEOUT_SENTINEL"),
    )
    parent.send.return_value = seed
    adapter._client.fetch_channel.side_effect = [
        parent,
        _FakeThread(
            seed.id,
            discord.ChannelType.text,
            parent_id=999,
            guild_id=parent.guild.id,
        ),
    ]

    result = await adapter._create_thread(_interaction(parent), name="Planning")

    assert "error" in result
    assert "RAW_TIMEOUT_SENTINEL" not in result["error"]
    parent.send.assert_awaited_once()
    parent.create_thread.assert_not_awaited()
    seed.create_thread.assert_awaited_once()
    seed.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_reconciles_committed_thread_without_deleting_seed(adapter):
    parent = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    parent.create_thread = AsyncMock()
    seed = _owned_seed(
        parent,
        create_error=TimeoutError("RAW_HANDOFF_TIMEOUT_SENTINEL"),
    )
    parent.send.return_value = seed
    thread = _FakeThread(
        seed.id,
        discord.ChannelType.text,
        parent_id=parent.id,
        guild_id=parent.guild.id,
    )
    adapter._client.get_channel.return_value = parent
    adapter._client.fetch_channel.side_effect = [parent, thread]

    result = await adapter.create_handoff_thread(str(parent.id), "Planning")

    assert result == str(seed.id)
    parent.send.assert_awaited_once()
    parent.create_thread.assert_not_awaited()
    seed.create_thread.assert_awaited_once()
    seed.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rate_limited", [False, True], ids=["timeout", "rate-limit"])
async def test_handoff_forum_write_replay_boundary(adapter, monkeypatch, rate_limited):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    thread = _FakeThread(
        888,
        discord.ChannelType.forum,
        parent_id=200,
        guild_id=1,
    )
    create_effect = TimeoutError("RAW_FORUM_HANDOFF_SENTINEL")
    if rate_limited:
        create_effect = [
            _TestHTTPException(
                _FakeResponse(429, retry_after="2.5"),
                "RAW_HANDOFF_429_SENTINEL",
            ),
            thread,
        ]
    parent = SimpleNamespace(
        id=200,
        create_thread=AsyncMock(side_effect=create_effect),
        send=AsyncMock(),
    )
    adapter._client.get_channel.return_value = parent
    adapter._client.fetch_channel.return_value = parent
    monkeypatch.setattr(adapter, "_is_forum_or_media_channel", lambda _channel: True)

    result = await adapter.create_handoff_thread(str(parent.id), "Planning")

    parent.send.assert_not_awaited()
    if rate_limited:
        assert result == str(thread.id)
        assert parent.create_thread.await_count == 2
        sleep.assert_awaited_once_with(2.5)
    else:
        assert result is None
        parent.create_thread.assert_awaited_once()
        sleep.assert_not_awaited()
