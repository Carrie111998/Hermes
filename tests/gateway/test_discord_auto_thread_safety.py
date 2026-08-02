"""Safety contracts for Discord auto-thread routing and REST retries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from gateway.config import PlatformConfig
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

    def __init__(self, channel_id: int, parent_type: discord.ChannelType):
        parent = SimpleNamespace(
            id=100,
            name="parent",
            topic="Parent topic",
            type=parent_type,
        )
        guild = SimpleNamespace(
            id=1,
            name="TestGuild",
            get_channel=lambda channel_id: parent if channel_id == parent.id else None,
        )
        self.id = channel_id
        self.name = "existing-post"
        self.guild = guild
        self.parent_id = parent.id

    def history(self, *args, **kwargs):
        async def _empty():
            return
            yield  # pragma: no cover - make this an async generator

        return _empty()


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
    adapter._discord_message_admission = MagicMock(return_value=(True, False))

    message = _message(projected_channel, guild=None)
    thread = SimpleNamespace(id=300, name="new-thread")

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
    adapter._discord_message_admission = MagicMock(return_value=(True, False))
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
    adapter._discord_message_admission = MagicMock(return_value=(True, False))
    adapter._auto_create_thread = AsyncMock()
    adapter._threads.mark("200")

    dispatched = await adapter._dispatch_recovered_message(
        _message(projected_channel, guild=projected_channel.guild)
    )

    assert dispatched is True
    adapter._client.fetch_channel.assert_awaited_once_with(200)
    adapter._auto_create_thread.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "thread"
    assert event.source.chat_id == "200"


@pytest.mark.asyncio
async def test_authoritative_channel_resolution_is_cached(adapter):
    projected_channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    authoritative_thread = _FakeThread(200, discord.ChannelType.forum)
    adapter._client.fetch_channel.return_value = authoritative_thread
    first = _message(projected_channel, guild=projected_channel.guild)
    second = _message(projected_channel, guild=projected_channel.guild)

    (
        first_channel,
        first_authoritative,
    ) = await adapter._resolve_authoritative_message_channel(first)
    (
        second_channel,
        second_authoritative,
    ) = await adapter._resolve_authoritative_message_channel(second)

    assert first_authoritative is True
    assert second_authoritative is True
    assert first_channel is authoritative_thread
    assert second_channel is authoritative_thread
    adapter._client.fetch_channel.assert_awaited_once_with(200)


@pytest.mark.asyncio
async def test_channel_resolution_runs_only_after_admission(adapter):
    adapter._discord_message_admission = MagicMock(return_value=(False, False))

    dispatched = await adapter._dispatch_discord_message(
        _message(_FakeTextChannel(200, guild=None), guild=None)
    )

    assert dispatched is False
    adapter._client.fetch_channel.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolved_channel_context_fails_closed_with_one_honest_notice(
    adapter,
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)
    adapter._discord_message_admission = MagicMock(return_value=(True, False))
    adapter._client.fetch_channel.side_effect = _http_error(403)
    projected_channel = _FakeTextChannel(200, guild=None)
    message = _message(projected_channel, guild=None)
    message.create_thread = AsyncMock()

    dispatched = await adapter._dispatch_discord_message(message)

    assert dispatched is False
    adapter._client.fetch_channel.assert_awaited_once_with(200)
    message.create_thread.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    projected_channel.send.assert_awaited_once()
    notice = projected_channel.send.await_args.args[0]
    assert "could not create" in notice.lower()
    assert "thread" in notice.lower()
    assert "thread created" not in notice.lower()


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
    channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    message = _message(channel, guild=channel.guild)
    message.create_thread = AsyncMock(side_effect=error)

    result = await adapter._auto_create_thread(message)

    assert result is None
    message.create_thread.assert_awaited_once()
    sleep.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_delay"),
    [
        (_http_error(500), 0.75),
        (_http_error(503), 0.75),
        (aiohttp.ClientConnectionError("connection lost"), 0.75),
        (TimeoutError("request timed out"), 0.75),
    ],
    ids=["http-500", "http-503", "network", "timeout"],
)
async def test_transient_auto_thread_failures_retry_once(
    adapter,
    monkeypatch,
    error,
    expected_delay,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    message = _message(channel, guild=channel.guild)
    thread = SimpleNamespace(id=300, name="new-thread")
    message.create_thread = AsyncMock(side_effect=[error, thread])

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    sleep.assert_awaited_once_with(expected_delay)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limit_retry_honors_retry_after(adapter, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    message = _message(channel, guild=channel.guild)
    thread = SimpleNamespace(id=300, name="new-thread")
    message.create_thread = AsyncMock(
        side_effect=[_http_error(429, retry_after="2.5"), thread]
    )

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    sleep.assert_awaited_once_with(2.5)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_rate_limited_retry_honors_retry_after(adapter, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    message = _message(channel, guild=channel.guild)
    thread = SimpleNamespace(id=300, name="new-thread")
    message.create_thread = AsyncMock(
        side_effect=[_TestRateLimited(retry_after=4.0), thread]
    )

    result = await adapter._auto_create_thread(message)

    assert result is thread
    assert message.create_thread.await_count == 2
    sleep.assert_awaited_once_with(4.0)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_transient_retries_never_create_duplicate_seeds(
    adapter,
    monkeypatch,
):
    sleep = AsyncMock()
    monkeypatch.setattr(discord_platform.asyncio, "sleep", sleep)
    channel = _FakeTextChannel(200, guild=SimpleNamespace(id=1))
    message = _message(channel, guild=channel.guild)
    message.create_thread = AsyncMock(
        side_effect=[
            aiohttp.ClientConnectionError("first failure"),
            aiohttp.ClientConnectionError("second failure"),
        ]
    )

    result = await adapter._auto_create_thread(message)

    assert result is None
    assert message.create_thread.await_count == 2
    sleep.assert_awaited_once_with(0.75)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_fallback_seed_is_honest_and_removed_if_orphaned(adapter):
    seed = SimpleNamespace(
        id=777,
        create_thread=AsyncMock(side_effect=ValueError("missing guild")),
        delete=AsyncMock(),
    )
    parent = SimpleNamespace(
        create_thread=AsyncMock(side_effect=RuntimeError("direct failed")),
        send=AsyncMock(return_value=seed),
    )
    interaction = SimpleNamespace(
        channel=parent,
        channel_id=200,
        user=SimpleNamespace(display_name="Jezza"),
    )

    result = await adapter._create_thread(interaction, name="Planning")

    assert "error" in result
    parent.send.assert_awaited_once_with(
        "\U0001f9f5 Thread requested via Hermes: **Planning**"
    )
    seed.create_thread.assert_awaited_once()
    seed.delete.assert_awaited_once()
