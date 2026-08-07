"""Discord interaction-scoped upload limits and follow-up delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
import plugins.platforms.discord.adapter as discord_adapter_module
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    DiscordInteractionDeliveryContext,
    _interaction_attachment_size_limit,
)


MiB = 1024 * 1024


class _DeliveryMetadataAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.document_metadata = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send_document(
        self,
        chat_id,
        file_path,
        caption=None,
        file_name=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ):
        self.document_metadata = metadata
        return SendResult(success=True, message_id="document")


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    if stop_event is not None:
        await stop_event.wait()


def _adapter_with_channels(*channels):
    by_id = {int(channel.id): channel for channel in channels}
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(
        get_channel=lambda channel_id: by_id.get(int(channel_id)),
        fetch_channel=AsyncMock(
            side_effect=lambda channel_id: by_id.get(int(channel_id))
        ),
    )
    adapter._is_forum_parent = lambda _channel: False
    return adapter


def _context(interaction, limit):
    return DiscordInteractionDeliveryContext(
        interaction=interaction,
        attachment_size_limit=limit,
        channel_id=str(interaction.channel_id),
        created_at=0.0,
    )


def _message(message_id, filename="file.bin"):
    return SimpleNamespace(
        id=message_id,
        attachments=[SimpleNamespace(filename=filename)],
    )


@pytest.mark.asyncio
async def test_message_event_delivery_context_reaches_media_metadata(
    tmp_path, monkeypatch
):
    root = tmp_path / "media"
    document = root / "report.bin"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"payload")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (root.resolve(),)
    )

    marker = object()
    adapter = _DeliveryMetadataAdapter()
    adapter._keep_typing = _hold_typing
    adapter.set_message_handler(
        AsyncMock(return_value=f"Here it is.\nMEDIA:{document.resolve()}")
    )
    event = MessageEvent(
        text="send the report",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="100", chat_type="group"
        ),
        metadata={"delivery_context": marker},
    )

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.document_metadata["delivery_context"] is marker


@pytest.mark.asyncio
async def test_streaming_media_keeps_message_event_delivery_context(
    tmp_path, monkeypatch
):
    root = tmp_path / "stream-media"
    document = root / "streamed.bin"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"payload")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS", (root.resolve(),)
    )

    marker = object()
    adapter = _DeliveryMetadataAdapter()
    event = MessageEvent(
        text="send the report",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="100", chat_type="group"
        ),
        metadata={"delivery_context": marker},
    )
    runner = object.__new__(GatewayRunner)

    await runner._deliver_media_from_response(
        f"Done.\nMEDIA:{document.resolve()}", event, adapter
    )

    assert adapter.document_metadata["delivery_context"] is marker


def test_interaction_limit_uses_discordpy_filesize_limit_exactly():
    interaction = SimpleNamespace(filesize_limit=524_288_000)

    assert _interaction_attachment_size_limit(interaction) == 524_288_000


def test_interaction_limit_supports_discord_wire_attribute_name():
    interaction = SimpleNamespace(attachment_size_limit=524_288_000)

    assert _interaction_attachment_size_limit(interaction) == 524_288_000


@pytest.mark.parametrize("value", [None, 0, -1, True, "not-an-int"])
def test_interaction_limit_rejects_missing_or_invalid_values(value):
    interaction = SimpleNamespace(filesize_limit=value)

    assert _interaction_attachment_size_limit(interaction) is None


@pytest.mark.asyncio
async def test_interaction_followup_selected_under_effective_limit(
    tmp_path, monkeypatch
):
    path = tmp_path / "large.bin"
    path.write_bytes(b"test")
    monkeypatch.setattr("os.path.getsize", lambda _path: 100 * MiB)

    interaction = SimpleNamespace(
        channel_id=101,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(return_value=_message(9001, path.name))
        ),
    )
    channel = SimpleNamespace(id=101, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "101",
        str(path),
        metadata={"delivery_context": _context(interaction, 500 * MiB)},
    )

    assert result.success is True
    assert result.message_id == "9001"
    interaction.followup.send.assert_awaited_once()
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["wait"] is True
    assert len(kwargs["files"]) == 1
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_over_limit_fails_before_discord_io(tmp_path, monkeypatch):
    path = tmp_path / "too-large.bin"
    path.write_bytes(b"test")
    monkeypatch.setattr("os.path.getsize", lambda _path: 60 * MiB)

    interaction = SimpleNamespace(
        channel_id=102,
        is_expired=lambda: False,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    channel = SimpleNamespace(id=102, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "102",
        str(path),
        metadata={"delivery_context": _context(interaction, 50 * MiB)},
    )

    assert result.success is False
    assert "60.0 MiB" in (result.error or "")
    assert "50.0 MiB" in (result.error or "")
    interaction.followup.send.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_interaction_limit_keeps_channel_send_path(tmp_path):
    path = tmp_path / "small-no-limit.bin"
    path.write_bytes(b"small")
    interaction = SimpleNamespace(
        channel_id=107,
        is_expired=lambda: False,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    channel = SimpleNamespace(
        id=107, send=AsyncMock(return_value=_message(9006, path.name))
    )
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "107",
        str(path),
        metadata={"delivery_context": _context(interaction, None)},
    )

    assert result.success is True
    interaction.followup.send.assert_not_awaited()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_interaction_keeps_channel_send_path(tmp_path):
    path = tmp_path / "small.bin"
    path.write_bytes(b"small")
    channel = SimpleNamespace(
        id=103, send=AsyncMock(return_value=_message(9002, path.name))
    )
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment("103", str(path))

    assert result.success is True
    assert result.message_id == "9002"
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_followup_falls_back_once_to_channel_send(tmp_path):
    class UnknownWebhook(Exception):
        status = 404
        code = 10015

    path = tmp_path / "small.bin"
    path.write_bytes(b"small")
    interaction = SimpleNamespace(
        channel_id=104,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(side_effect=UnknownWebhook("Unknown Webhook"))
        ),
    )
    channel = SimpleNamespace(
        id=104, send=AsyncMock(return_value=_message(9003, path.name))
    )
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "104",
        str(path),
        metadata={"delivery_context": _context(interaction, 500 * MiB)},
    )

    assert result.success is True
    assert result.message_id == "9003"
    interaction.followup.send.assert_awaited_once()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_interaction_does_not_try_oversized_normal_fallback(
    tmp_path, monkeypatch
):
    path = tmp_path / "large-expired.bin"
    path.write_bytes(b"small fixture")
    monkeypatch.setattr("os.path.getsize", lambda _path: 100 * MiB)
    monkeypatch.setattr(
        discord_adapter_module,
        "discord",
        SimpleNamespace(utils=SimpleNamespace(DEFAULT_FILE_SIZE_LIMIT_BYTES=10 * MiB)),
    )

    interaction = SimpleNamespace(
        channel_id=108,
        is_expired=lambda: True,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    channel = SimpleNamespace(id=108, guild=None, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "108",
        str(path),
        metadata={"delivery_context": _context(interaction, 500 * MiB)},
    )

    assert result.success is False
    assert "100.0 MiB" in result.error
    assert "normal Discord channel path allows 10.0 MiB" in result.error
    interaction.followup.send.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_generated_image_batch_uses_interaction_followup(tmp_path, monkeypatch):
    image = tmp_path / "large.png"
    image.write_bytes(b"png")
    monkeypatch.setattr("os.path.getsize", lambda _path: 100 * MiB)

    interaction = SimpleNamespace(
        channel_id=106,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(return_value=_message(9005, image.name))
        ),
    )
    channel = SimpleNamespace(id=106, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    await adapter.send_multiple_images(
        "106",
        [(image.as_uri(), "generated image")],
        metadata={"delivery_context": _context(interaction, 500 * MiB)},
    )

    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["wait"] is True
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_attachment_verification_fails_loud(tmp_path):
    path = tmp_path / "missing-after-send.bin"
    path.write_bytes(b"small")
    interaction = SimpleNamespace(
        channel_id=105,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=9004, attachments=[]))
        ),
    )
    channel = SimpleNamespace(id=105, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    result = await adapter._send_file_attachment(
        "105",
        str(path),
        metadata={"delivery_context": _context(interaction, 500 * MiB)},
    )

    assert result.success is False
    assert "no files" in (result.error or "").lower()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_contexts_are_isolated_between_concurrent_requests(
    tmp_path, monkeypatch
):
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")
    monkeypatch.setattr("os.path.getsize", lambda _path: 100 * MiB)

    interaction_a = SimpleNamespace(
        channel_id=201,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(return_value=_message(9201, path_a.name))
        ),
    )
    interaction_b = SimpleNamespace(
        channel_id=202,
        is_expired=lambda: False,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    channel_a = SimpleNamespace(id=201, send=AsyncMock())
    channel_b = SimpleNamespace(id=202, send=AsyncMock())
    adapter = _adapter_with_channels(channel_a, channel_b)

    result_a, result_b = await asyncio.gather(
        adapter._send_file_attachment(
            "201",
            str(path_a),
            metadata={"delivery_context": _context(interaction_a, 500 * MiB)},
        ),
        adapter._send_file_attachment(
            "202",
            str(path_b),
            metadata={"delivery_context": _context(interaction_b, 10 * MiB)},
        ),
    )

    assert result_a.success is True
    assert result_b.success is False
    interaction_a.followup.send.assert_awaited_once()
    interaction_b.followup.send.assert_not_awaited()
    channel_a.send.assert_not_awaited()
    channel_b.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversational_slash_defers_non_ephemeral_and_dispatches_user_text():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._check_slash_authorization = AsyncMock(return_value=True)
    adapter.handle_message = AsyncMock()
    channel = SimpleNamespace(
        id=301, name="general", guild=SimpleNamespace(name="Test")
    )
    interaction = SimpleNamespace(
        id=777,
        channel=channel,
        channel_id=301,
        guild_id=401,
        user=SimpleNamespace(id=501, name="NitroUser", display_name="NitroUser"),
        filesize_limit=500 * MiB,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await adapter._run_conversational_slash(interaction, "Create a file")

    interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
    interaction.edit_original_response.assert_awaited_once_with(content="Working…")
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Create a file"
    assert event.message_type is MessageType.TEXT
    assert event.raw_message is interaction
    assert event.metadata["cleanup_original_interaction_response"] is True
    context = event.metadata["delivery_context"]
    assert context.interaction is interaction
    assert context.attachment_size_limit == 500 * MiB


@pytest.mark.asyncio
async def test_conversational_slash_still_dispatches_when_working_edit_fails():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._check_slash_authorization = AsyncMock(return_value=True)
    adapter.handle_message = AsyncMock()
    channel = SimpleNamespace(
        id=302, name="general", guild=SimpleNamespace(name="Test")
    )
    interaction = SimpleNamespace(
        id=778,
        channel=channel,
        channel_id=302,
        guild_id=402,
        user=SimpleNamespace(id=502, name="NitroUser", display_name="NitroUser"),
        filesize_limit=500 * MiB,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(side_effect=TimeoutError("Discord timeout")),
    )

    await adapter._run_conversational_slash(interaction, "Create a file")

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.metadata["cleanup_original_interaction_response"] is True
    # A failed compatibility edit means followups might still replace the original
    # deferred response, so file delivery must fall back to the channel path.
    assert event.metadata["delivery_context"].attachment_size_limit is None


@pytest.mark.asyncio
async def test_interaction_audio_uses_followup_attachment_path(tmp_path):
    path = tmp_path / "clip.ogg"
    path.write_bytes(b"audio")
    interaction = SimpleNamespace(
        channel_id=109,
        is_expired=lambda: False,
        followup=SimpleNamespace(
            send=AsyncMock(return_value=_message(9401, path.name))
        ),
    )
    channel = SimpleNamespace(id=109, send=AsyncMock())
    adapter = _adapter_with_channels(channel)

    result = await adapter.send_voice(
        "109",
        str(path),
        metadata={"delivery_context": _context(interaction, 50 * MiB)},
    )

    assert result.success is True
    interaction.followup.send.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_interaction_request_uses_fifo_without_metadata_merge():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(_pending_messages={})
    runner.adapters = {Platform.DISCORD: adapter}
    runner._draining = False
    runner._is_user_authorized = lambda _source: True
    queued = []
    runner._queue_or_replace_pending_event = lambda key, event: queued.append((key, event))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="301",
        chat_type="channel",
        user_id="501",
    )
    event = MessageEvent(
        text="Create a file",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"delivery_context": object()},
    )

    handled = await runner._handle_active_session_busy_message(event, "discord:301")

    assert handled is True
    assert queued == [("discord:301", event)]


def test_interaction_request_never_merges_with_adjacent_media():
    runner = object.__new__(GatewayRunner)
    queued = []
    runner._queue_depth = lambda *_args, **_kwargs: 0
    runner._enqueue_fifo = lambda key, event, adapter: queued.append((key, event))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="301",
        chat_type="channel",
        user_id="501",
    )
    interaction_event = MessageEvent(
        text="Create a file",
        message_type=MessageType.TEXT,
        source=source,
        metadata={"delivery_context": object()},
    )
    photo_event = MessageEvent(
        text="ordinary photo",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/photo.png"],
        media_types=["image/png"],
    )

    for existing, incoming in (
        (photo_event, interaction_event),
        (interaction_event, photo_event),
    ):
        adapter = SimpleNamespace(_pending_messages={"discord:301": existing})
        runner.adapters = {Platform.DISCORD: adapter}
        queued.clear()

        runner._queue_or_replace_pending_event("discord:301", incoming)

        assert adapter._pending_messages["discord:301"] is existing
        assert queued == [("discord:301", incoming)]
        assert interaction_event.metadata.get("delivery_context") is not None


@pytest.mark.asyncio
async def test_processing_complete_deletes_only_original_deferred_response(monkeypatch):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    monkeypatch.setattr(
        adapter, "_record_discord_processing_complete", lambda *_args: None
    )
    interaction = SimpleNamespace(delete_original_response=AsyncMock())
    event = SimpleNamespace(
        raw_message=interaction,
        metadata={"cleanup_original_interaction_response": True},
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    interaction.delete_original_response.assert_awaited_once()
