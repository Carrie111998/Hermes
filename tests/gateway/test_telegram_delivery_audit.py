"""Metadata-only Telegram delivery correlation coverage."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult, _thread_metadata_for_event
from gateway.platforms.base import Platform, SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


CORRELATION = "a1b2c3d4e5f60708"


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=321)
    )
    adapter._bot.edit_message_text = AsyncMock()
    adapter._send_path_degraded = False
    adapter._should_attempt_rich = MagicMock(return_value=False)
    adapter.send_typing = AsyncMock()
    return adapter


def test_event_metadata_is_carried_into_outbound_metadata():
    event = MessageEvent(
        text="redacted",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="not-logged",
            chat_type="dm",
        ),
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    metadata = _thread_metadata_for_event(event)

    assert metadata["telegram_delivery_correlation_id"] == CORRELATION
    assert "redacted" not in repr(metadata)
    assert "not-logged" not in repr(metadata)


def test_batched_event_metadata_carries_all_correlation_ids():
    adapter = _adapter()
    first = MessageEvent(
        text="first",
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )
    second_id = "1122334455667788"
    second = MessageEvent(
        text="second",
        metadata={"telegram_delivery_correlation_id": second_id},
    )

    adapter._merge_delivery_correlation_metadata(first, second)
    metadata = _thread_metadata_for_event(first)

    assert metadata["telegram_delivery_correlation_id"] == CORRELATION
    assert metadata["telegram_delivery_correlation_ids"] == [CORRELATION, second_id]


def test_batched_event_emits_one_outbound_record_per_correlation_id(caplog):
    adapter = _adapter()
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")
    second_id = "1122334455667788"

    adapter._audit_send_result(
        SendResult(success=True, message_id="321"),
        {
            "telegram_delivery_correlation_id": CORRELATION,
            "telegram_delivery_correlation_ids": [CORRELATION, second_id],
        },
    )

    assert caplog.text.count("phase=outbound") == 2
    assert f"correlation_id={CORRELATION}" in caplog.text
    assert f"correlation_id={second_id}" in caplog.text


def test_audit_result_is_noop_without_correlation(caplog):
    adapter = _adapter()

    result = adapter._audit_send_result(
        SendResult(success=True, message_id="321"),
        {"notify": True},
    )

    assert result.success is True
    assert "delivery_audit" not in caplog.text


@pytest.mark.asyncio
async def test_correlated_send_emits_metadata_only_success(caplog):
    adapter = _adapter()
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await adapter.send(
        "chat-not-logged",
        "secret message body must not be logged",
        metadata={
            "telegram_delivery_correlation_id": CORRELATION,
            "notify": True,
        },
    )

    assert result.success is True
    assert result.message_id == "321"
    assert "phase=outbound" in caplog.text
    assert "status=delivered" in caplog.text
    assert f"correlation_id={CORRELATION}" in caplog.text
    assert "321" in caplog.text
    assert "secret message body" not in caplog.text
    assert "chat-not-logged" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "path_keyword"),
    [
        ("send_document", "file_path"),
        ("send_video", "video_path"),
        ("send_image_file", "image_path"),
    ],
)
async def test_correlated_native_file_sends_emit_success(
    caplog, method_name, path_keyword
):
    adapter = _adapter()
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(
        return_value=SimpleNamespace(message_id=654)
    )
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await getattr(adapter, method_name)(
        "chat-not-logged",
        **{path_keyword: "pyproject.toml"},
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert result.success is True
    assert result.message_id == "654"
    assert caplog.text.count("phase=outbound") == 1
    assert f"correlation_id={CORRELATION}" in caplog.text
    assert "chat-not-logged" not in caplog.text


@pytest.mark.asyncio
async def test_native_media_fallback_emits_one_audit_record(caplog):
    adapter = _adapter()
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(
        side_effect=RuntimeError("native send failed")
    )
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await adapter.send_document(
        "chat-not-logged",
        file_path="pyproject.toml",
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert result.success is True
    assert caplog.text.count("phase=outbound") == 1
    assert f"correlation_id={CORRELATION}" in caplog.text


@pytest.mark.asyncio
async def test_correlated_voice_send_emits_success(tmp_path, caplog):
    adapter = _adapter()
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"test voice payload")
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(
        return_value=SimpleNamespace(message_id=755)
    )
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await adapter.send_voice(
        "chat-not-logged",
        str(audio_path),
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert result.success is True
    assert result.message_id == "755"
    assert caplog.text.count("phase=outbound") == 1
    assert f"correlation_id={CORRELATION}" in caplog.text


@pytest.mark.asyncio
async def test_correlated_animation_send_emits_success(caplog):
    adapter = _adapter()
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(
        return_value=SimpleNamespace(message_id=856)
    )
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await adapter.send_animation(
        "chat-not-logged",
        "https://example.invalid/animation.gif",
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert result.success is True
    assert result.message_id == "856"
    assert caplog.text.count("phase=outbound") == 1
    assert f"correlation_id={CORRELATION}" in caplog.text


@pytest.mark.asyncio
async def test_correlated_media_group_response_emits_success(caplog):
    adapter = _adapter()
    adapter._send_with_dm_topic_reply_anchor_retry = AsyncMock(
        return_value=[SimpleNamespace(message_id=957)]
    )
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    await adapter.send_multiple_images(
        "chat-not-logged",
        [("https://example.invalid/photo.jpg", "")],
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert caplog.text.count("phase=outbound") == 1
    assert "status=delivered" in caplog.text
    assert "message_id=957" in caplog.text
    assert f"correlation_id={CORRELATION}" in caplog.text


@pytest.mark.asyncio
async def test_correlated_edit_response_emits_success(caplog):
    adapter = _adapter()
    adapter._rich_eligible = MagicMock(return_value=False)
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")

    result = await adapter.edit_message(
        "chat-not-logged",
        "111",
        "secret edit body must not be logged",
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    assert result.success is True
    assert result.message_id == "111"
    assert caplog.text.count("phase=outbound") == 1
    assert f"correlation_id={CORRELATION}" in caplog.text
    assert "secret edit body" not in caplog.text
    assert "chat-not-logged" not in caplog.text


def test_correlated_inbound_audit_uses_update_metadata_only(caplog):
    adapter = _adapter()
    caplog.set_level("INFO", logger="plugins.platforms.telegram.adapter")
    event = MessageEvent(
        text="secret message body must not be logged",
        platform_update_id=9876,
        metadata={"telegram_delivery_correlation_id": CORRELATION},
    )

    adapter._log_inbound_accepted(event)

    assert "phase=inbound" in caplog.text
    assert "status=accepted" in caplog.text
    assert f"correlation_id={CORRELATION}" in caplog.text
    assert "update_id=9876" in caplog.text
    assert "secret message body" not in caplog.text
