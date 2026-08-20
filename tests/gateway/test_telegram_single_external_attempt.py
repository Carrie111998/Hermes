from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


class FloodError(Exception):
    retry_after = 30.0


def _adapter_with_send(side_effect) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._should_attempt_rich = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=side_effect))
    return adapter


def test_adapter_advertises_single_external_attempt_capability() -> None:
    assert TelegramAdapter.supports_single_external_attempt is True


@pytest.mark.asyncio
async def test_single_external_attempt_disables_internal_network_retry(monkeypatch) -> None:
    adapter = _adapter_with_send(NetworkError("connection lost after write"))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_send_keeps_existing_network_retry_behavior(monkeypatch) -> None:
    adapter = _adapter_with_send(
        [
            NetworkError("first failure"),
            NetworkError("second failure"),
            SimpleNamespace(message_id=901),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send("12345", "hello", metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "901"
    assert adapter._bot.send_message.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_single_external_attempt_does_not_sleep_for_flood_retry(monkeypatch) -> None:
    adapter = _adapter_with_send(FloodError("retry after 30"))
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.telegram.adapter.asyncio.sleep", sleep)

    result = await adapter.send(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_preserves_safe_deleted_reply_fallback() -> None:
    adapter = _adapter_with_send(
        [
            BadRequest("Message to be replied not found"),
            SimpleNamespace(message_id=902),
        ]
    )

    result = await adapter.send(
        "12345",
        "hello",
        reply_to="999",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is True
    assert result.message_id == "902"
    assert adapter._bot.send_message.await_count == 2
    calls = adapter._bot.send_message.await_args_list
    assert calls[0].kwargs["reply_to_message_id"] == 999
    assert calls[1].kwargs["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_single_external_attempt_preserves_safe_markdown_parse_fallback() -> None:
    adapter = _adapter_with_send(
        [
            BadRequest("can't parse entities"),
            SimpleNamespace(message_id=903),
        ]
    )

    result = await adapter.send(
        "12345",
        "hello _world_",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is True
    assert result.message_id == "903"
    assert adapter._bot is not None
    calls = adapter._bot.send_message.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["parse_mode"] is not None
    assert calls[1].kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_shared_send_wrapper_honors_single_external_attempt(monkeypatch) -> None:
    adapter = _adapter_with_send(NetworkError("connection lost after write"))
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.platforms.base.asyncio.sleep", sleep)

    result = await adapter._send_with_retry(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
        max_retries=2,
        base_delay=0,
    )

    assert result.success is False
    assert adapter._bot.send_message.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_rejects_multi_chunk_before_first_send() -> None:
    adapter = _adapter_with_send(SimpleNamespace(message_id=903))
    object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 12)

    result = await adapter.send(
        "12345",
        "this response requires multiple Telegram chunks",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result.success is False
    assert result.error_kind == "too_long"
    assert adapter._bot is not None
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_wrapper_never_fallback_sends_for_single_attempt() -> None:
    adapter = _adapter_with_send(SimpleNamespace(message_id=904))
    first_failure = SendResult(
        success=False,
        error="definitive formatting rejection",
        error_kind="formatting",
        retryable=False,
    )
    adapter.send = AsyncMock(
        side_effect=[first_failure, SendResult(success=True, message_id="duplicate")]
    )

    result = await adapter._send_with_retry(
        "12345",
        "hello",
        metadata={"single_external_attempt": True, "notify": True},
    )

    assert result is first_failure
    assert adapter.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "bot_method_name", "path_kw", "filename", "payload"),
    [
        ("send_image_file", "send_photo", "image_path", "photo.png", b"png-data"),
        ("send_document", "send_document", "file_path", "report.txt", b"report-data"),
        ("send_video", "send_video", "video_path", "clip.mp4", b"video-data"),
    ],
)
async def test_single_external_attempt_disables_stale_dm_topic_media_retry(
    tmp_path,
    method_name,
    bot_method_name,
    path_kw,
    filename,
    payload,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    media_path = tmp_path / filename
    media_path.write_bytes(payload)
    adapter._bot = SimpleNamespace(
        **{
            bot_method_name: AsyncMock(
                side_effect=BadRequest("Message to be replied not found")
            )
        }
    )

    result = await getattr(adapter, method_name)(
        chat_id="12345",
        **{path_kw: str(media_path)},
        metadata={
            "single_external_attempt": True,
            "thread_id": "20197",
            "telegram_dm_topic_reply_fallback": True,
            "telegram_reply_to_message_id": "462",
        },
    )

    assert result.success is False
    getattr(adapter._bot, bot_method_name).assert_awaited_once()


@pytest.mark.asyncio
async def test_single_external_attempt_disables_media_group_per_image_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"png-data")
    adapter._bot = SimpleNamespace(
        send_media_group=AsyncMock(side_effect=RuntimeError("album rejected"))
    )
    fallback = AsyncMock()
    monkeypatch.setattr(BasePlatformAdapter, "send_multiple_images", fallback)

    await adapter.send_multiple_images(
        chat_id="12345",
        images=[(f"file://{image_path}", "caption")],
        metadata={"single_external_attempt": True},
    )

    adapter._bot.send_media_group.assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_disables_image_file_document_fallback(
    tmp_path,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"png-data")
    adapter._bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=RuntimeError("photo rejected"))
    )
    adapter.send_document = AsyncMock(return_value=SendResult(success=True))

    result = await adapter.send_image_file(
        chat_id="12345",
        image_path=str(image_path),
        metadata={"single_external_attempt": True},
    )

    assert result.success is False
    adapter._bot.send_photo.assert_awaited_once()
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "bot_method_name", "path_kw", "filename", "payload", "base_method"),
    [
        (
            "send_document",
            "send_document",
            "file_path",
            "report.txt",
            b"report-data",
            "send_document",
        ),
        ("send_video", "send_video", "video_path", "clip.mp4", b"video-data", "send_video"),
    ],
)
async def test_single_external_attempt_disables_document_video_base_fallback(
    monkeypatch,
    tmp_path,
    method_name,
    bot_method_name,
    path_kw,
    filename,
    payload,
    base_method,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    media_path = tmp_path / filename
    media_path.write_bytes(payload)
    adapter._bot = SimpleNamespace(
        **{bot_method_name: AsyncMock(side_effect=RuntimeError("upload rejected"))}
    )
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="fallback"))
    monkeypatch.setattr(BasePlatformAdapter, base_method, fallback)

    result = await getattr(adapter, method_name)(
        chat_id="12345",
        **{path_kw: str(media_path)},
        metadata={"single_external_attempt": True},
    )

    assert result.success is False
    getattr(adapter._bot, bot_method_name).assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_disables_url_photo_download_upload_fallback(
    monkeypatch,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=RuntimeError("url rejected"))
    )
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="fallback"))

    def fail_client(**_kwargs):
        raise AssertionError("single-attempt URL photo must not download fallback")

    monkeypatch.setattr(BasePlatformAdapter, "send_image", fallback)
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda _url: True)
    monkeypatch.setattr("tools.url_safety.create_ssrf_safe_async_client", fail_client)

    result = await adapter.send_image(
        chat_id="12345",
        image_url="https://example.com/photo.png",
        metadata={"single_external_attempt": True},
    )

    assert result.success is False
    adapter._bot.send_photo.assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_external_attempt_disables_animation_photo_fallback() -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = SimpleNamespace(
        send_animation=AsyncMock(side_effect=RuntimeError("animation rejected"))
    )
    adapter.send_image = AsyncMock(return_value=SendResult(success=True, message_id="photo"))

    result = await adapter.send_animation(
        chat_id="12345",
        animation_url="https://example.com/animated.gif",
        metadata={"single_external_attempt": True},
    )

    assert result.success is False
    adapter._bot.send_animation.assert_awaited_once()
    adapter.send_image.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "payload", "bot_method_name"),
    [
        ("clip.ogg", b"ogg-data", "send_voice"),
        ("clip.mp3", b"mp3-data", "send_audio"),
    ],
)
async def test_single_external_attempt_disables_voice_audio_base_fallback(
    monkeypatch,
    tmp_path,
    filename,
    payload,
    bot_method_name,
) -> None:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    audio_path = tmp_path / filename
    audio_path.write_bytes(payload)
    adapter._bot = SimpleNamespace(
        **{bot_method_name: AsyncMock(side_effect=RuntimeError("audio rejected"))}
    )
    fallback = AsyncMock(return_value=SendResult(success=True, message_id="fallback"))
    monkeypatch.setattr(BasePlatformAdapter, "send_voice", fallback)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter._probe_voice_duration_seconds",
        lambda _path: 7,
    )

    result = await adapter.send_voice(
        chat_id="12345",
        audio_path=str(audio_path),
        metadata={"single_external_attempt": True},
    )

    assert result.success is False
    getattr(adapter._bot, bot_method_name).assert_awaited_once()
    fallback.assert_not_awaited()
