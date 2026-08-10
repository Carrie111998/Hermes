from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult
from plugins.platforms.discord import adapter as discord_adapter
from plugins.platforms.discord.adapter import DiscordAdapter


class DummyAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return False

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="unused")

    async def get_chat_info(self, chat_id):
        return {}


class FakeAllowedMentions:
    def __init__(self, users=None, everyone=None, roles=None, replied_user=None):
        self.users = users or []
        self.everyone = everyone
        self.roles = roles
        self.replied_user = replied_user

    @classmethod
    def none(cls):
        return cls([], everyone=False, roles=False, replied_user=False)


class FakeObject:
    def __init__(self, id):
        self.id = id


class FakeThread:
    def __init__(self, thread_id="123456789012345679", name="actual name"):
        self.id = thread_id
        self.name = name
        self.archived = False
        self.send = AsyncMock()
        self.edit = AsyncMock()
        self.delete = AsyncMock()


@pytest.mark.asyncio
async def test_base_noninteractive_lifecycle_is_unsupported_by_default():
    adapter = object.__new__(DummyAdapter)

    assert await adapter.create_noninteractive_work_thread("parent", "work") is None
    assert await adapter.archive_noninteractive_work_thread(None) is False
    assert await adapter.delete_noninteractive_work_thread(None) is False
    result = await adapter.send_noninteractive_work_notification(
        None, "routine", event="success", chief_user_id="12345678901234567"
    )
    assert result.success is False
    assert result.error == "Not supported"


@pytest.mark.asyncio
async def test_discord_creates_noninteractive_thread_directly_and_returns_handle(monkeypatch):
    parent = SimpleNamespace(create_thread=AsyncMock())
    thread = FakeThread(name="actual Discord name")
    parent.create_thread.return_value = thread
    client = SimpleNamespace(get_channel=lambda channel_id: parent, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)

    handle = await adapter.create_noninteractive_work_thread(
        "123456789012345678", "work title", auto_archive_duration=60
    )

    assert handle.parent_channel_id == "123456789012345678"
    assert handle.thread_id == "123456789012345679"
    assert handle.thread_name == "actual Discord name"
    parent.create_thread.assert_awaited_once_with(
        name="work title", auto_archive_duration=60, reason="Hermes non-interactive work"
    )


@pytest.mark.asyncio
async def test_discord_uses_seed_message_fallback_with_mentions_suppressed(monkeypatch):
    thread = FakeThread()
    seed = SimpleNamespace(create_thread=AsyncMock(return_value=thread))
    parent = SimpleNamespace(
        create_thread=AsyncMock(side_effect=PermissionError("no direct permission")),
        send=AsyncMock(return_value=seed),
        guild=SimpleNamespace(id="777777777777777777"),
    )
    client = SimpleNamespace(get_channel=lambda channel_id: parent, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)

    handle = await adapter.create_noninteractive_work_thread(
        "123456789012345678", "work @everyone @here <@&88888888888888888>"
    )

    assert handle.thread_id == "123456789012345679"
    assert handle.guild_id == "777777777777777777"
    assert parent.send.await_args.args == (
        "🧵 Hermes non-interactive work: **work @everyone @here <@&88888888888888888>**",
    )
    allowed = parent.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.replied_user is False
    assert allowed.users == []


@pytest.mark.asyncio
async def test_discord_seed_fallback_deletes_orphan_when_thread_creation_fails(monkeypatch):
    seed = SimpleNamespace(create_thread=AsyncMock(side_effect=PermissionError("no thread permission")), delete=AsyncMock())
    parent = SimpleNamespace(
        create_thread=AsyncMock(side_effect=PermissionError("no direct permission")),
        send=AsyncMock(return_value=seed),
    )
    client = SimpleNamespace(get_channel=lambda channel_id: parent, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)

    assert await adapter.create_noninteractive_work_thread("123456789012345678", "work") is None

    seed.delete.assert_awaited_once_with(reason="Hermes non-interactive work cleanup")


@pytest.mark.asyncio
async def test_discord_long_notification_chunks_and_only_first_chunk_mentions(monkeypatch):
    thread = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle, "x" * 4500, event="failure", chief_user_id="12345678901234567"
    )

    assert result.success is True
    calls = thread.send.await_args_list
    assert len(calls) >= 3
    assert all(len(call.kwargs["content"]) <= 2000 for call in calls)
    assert calls[0].kwargs["allowed_mentions"].users[0].id == 12345678901234567
    assert all(call.kwargs["allowed_mentions"].users == [] for call in calls[1:])


@pytest.mark.asyncio
async def test_discord_origin_background_suppresses_all_mentions(monkeypatch):
    channel = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: channel, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    adapter._reply_to_mode = "off"
    adapter._last_self_message_id = {}
    adapter.config = SimpleNamespace(extra={})
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)

    result = await adapter.send(
        "123456789012345678",
        "<@99999999999999999> <@&88888888888888888> @everyone @here success",
        metadata={"_hermes_origin_background": True},
    )

    assert result.success is True
    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.replied_user is False
    assert allowed.users == []


@pytest.mark.asyncio
async def test_discord_origin_failure_explicitly_allows_only_valid_chief_mention(monkeypatch):
    channel = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: channel, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    adapter._reply_to_mode = "off"
    adapter._last_self_message_id = {}
    adapter.config = SimpleNamespace(extra={})
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)

    result = await adapter.send(
        "123456789012345678",
        "<@99999999999999999> <@&88888888888888888> @everyone @here failure",
        metadata={
            "_hermes_origin_failure": True,
            "_hermes_chief_user_id": "12345678901234567",
        },
    )

    assert result.success is True
    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.replied_user is False
    assert [user.id for user in allowed.users] == [12345678901234567]


@pytest.mark.asyncio
async def test_discord_long_notification_reports_later_chunk_failure(monkeypatch):
    thread = FakeThread()
    thread.send.side_effect = [SimpleNamespace(id="first"), RuntimeError("second failed")]
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle, "x" * 4500, event="success", chief_user_id="12345678901234567"
    )

    assert result.success is False
    assert "second failed" in result.error


@pytest.mark.asyncio
async def test_discord_rejects_invalid_archive_duration_before_creation(monkeypatch):
    parent = SimpleNamespace(create_thread=AsyncMock(), send=AsyncMock())
    client = SimpleNamespace(get_channel=lambda channel_id: parent, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)

    assert await adapter.create_noninteractive_work_thread(
        "123456789012345678", "work", auto_archive_duration=30
    ) is None
    parent.create_thread.assert_not_awaited()
    parent.send.assert_not_awaited()
    client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_archive_and_delete_are_idempotent(monkeypatch):
    thread = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    assert await adapter.archive_noninteractive_work_thread(handle) is True
    thread.edit.assert_awaited_once_with(archived=True, reason="Hermes non-interactive work cleanup")

    thread.delete.side_effect = RuntimeError("Unknown Channel")
    assert await adapter.delete_noninteractive_work_thread(handle) is True


@pytest.mark.asyncio
async def test_discord_archive_treats_thread_deleted_during_edit_as_idempotent(monkeypatch):
    thread = FakeThread()
    thread.edit.side_effect = RuntimeError("Unknown Thread")
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    assert await adapter.archive_noninteractive_work_thread(handle) is True


@pytest.mark.asyncio
async def test_discord_archive_and_delete_report_lookup_failures(monkeypatch):
    client = SimpleNamespace(
        get_channel=lambda channel_id: None,
        fetch_channel=AsyncMock(side_effect=PermissionError("forbidden")),
    )
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    assert await adapter.archive_noninteractive_work_thread(handle) is False
    assert await adapter.delete_noninteractive_work_thread(handle) is False


@pytest.mark.asyncio
async def test_discord_archive_and_delete_treat_missing_lookup_as_idempotent(monkeypatch):
    client = SimpleNamespace(
        get_channel=lambda channel_id: None,
        fetch_channel=AsyncMock(return_value=None),
    )
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    assert await adapter.archive_noninteractive_work_thread(handle) is True
    assert await adapter.delete_noninteractive_work_thread(handle) is True


@pytest.mark.asyncio
async def test_discord_archive_and_delete_report_api_failures(monkeypatch):
    thread = FakeThread()
    thread.edit.side_effect = RuntimeError("rate limited")
    thread.delete.side_effect = RuntimeError("rate limited")
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    assert await adapter.archive_noninteractive_work_thread(handle) is False
    assert await adapter.delete_noninteractive_work_thread(handle) is False


@pytest.mark.asyncio
async def test_discord_actionable_notification_mentions_only_chief(monkeypatch):
    thread = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle,
        "Task failed",
        event="failure",
        chief_user_id="12345678901234567",
    )

    assert result.success is True
    kwargs = thread.send.call_args.kwargs
    assert kwargs["content"] == "<@12345678901234567> Task failed"
    assert kwargs["allowed_mentions"].users[0].id == 12345678901234567
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["allowed_mentions"].replied_user is False


@pytest.mark.asyncio
async def test_discord_routine_notification_disables_mentions(monkeypatch):
    thread = FakeThread()
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    monkeypatch.setattr(discord_adapter.discord, "Object", FakeObject)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle,
        "Task succeeded",
        event="success",
        chief_user_id="12345678901234567",
    )

    assert result.success is True
    kwargs = thread.send.call_args.kwargs
    assert kwargs["content"] == "Task succeeded"
    assert kwargs["allowed_mentions"].users == []


@pytest.mark.asyncio
async def test_discord_notification_send_failure_returns_unsuccessful_result(monkeypatch):
    thread = FakeThread()
    thread.send.side_effect = RuntimeError("send failed")
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle, "Task failed", event="failure", chief_user_id="not-an-id"
    )

    assert result.success is False
    assert "send failed" in result.error


@pytest.mark.asyncio
async def test_discord_notification_ignores_malformed_chief_id_without_mentioning(monkeypatch):
    thread = FakeThread()
    thread.send.return_value = SimpleNamespace(id="message-id")
    client = SimpleNamespace(get_channel=lambda channel_id: thread, fetch_channel=AsyncMock())
    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter.platform = discord_adapter.Platform.DISCORD
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter.discord, "AllowedMentions", FakeAllowedMentions)
    handle = discord_adapter.NonInteractiveWorkThreadHandle("parent", "123456789012345679", "work")

    result = await adapter.send_noninteractive_work_notification(
        handle, "Task failed", event="failure", chief_user_id="<@bad>"
    )

    assert result.success is True
    kwargs = thread.send.call_args.kwargs
    assert kwargs["content"] == "Task failed"
    assert kwargs["allowed_mentions"].users == []
