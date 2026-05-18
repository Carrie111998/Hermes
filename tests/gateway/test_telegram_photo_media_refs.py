"""
Tests for WS-4 (WB 71eadbc7 / WB b17b83f4): hermes telegram adapter surfaces
photo file_id / getFile URL into agent text context so Bobby (and other
agents) can quote a stable, copyable reference into structured downstream
payloads (e.g. PA observation media_url).

Covers:
  * format_media_refs_text helper output shape
  * _build_telegram_file_url URL construction (token-based + custom base)
  * media_refs propagation through _enqueue_photo_event burst-merge
  * Text-context injection in the run.py main turn flow uses
    format_media_refs_text when event.media_refs is populated.
"""

from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    format_media_refs_text,
)
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# format_media_refs_text
# ---------------------------------------------------------------------------


def test_format_media_refs_text_empty_returns_empty_string():
    assert format_media_refs_text([]) == ""
    assert format_media_refs_text(None) == ""  # defensive


def test_format_media_refs_text_single_photo_emits_copyable_block():
    refs = [
        {
            "file_id": "AgACAg-test-file-id",
            "file_unique_id": "AQADtest",
            "file_url": "https://api.telegram.org/file/bot12345:ABC/photos/file_3.jpg",
            "file_path": "/tmp/cache/img_abc123.jpg",
            "mime_type": "image/jpeg",
        }
    ]
    text = format_media_refs_text(refs)
    assert "[media#1 image/jpeg" in text
    assert "file_id=AgACAg-test-file-id" in text
    assert "file_url=https://api.telegram.org/file/bot12345:ABC/photos/file_3.jpg" in text
    assert "file_path=/tmp/cache/img_abc123.jpg" in text
    # closing bracket on its own line per format contract
    assert text.endswith("]")


def test_format_media_refs_text_skips_missing_fields():
    refs = [{"file_id": "X", "mime_type": "image/png"}]
    text = format_media_refs_text(refs)
    assert "file_id=X" in text
    assert "file_url" not in text
    assert "file_path" not in text


def test_format_media_refs_text_multiple_photos_numbered():
    refs = [
        {"file_id": "AAA", "mime_type": "image/jpeg"},
        {"file_id": "BBB", "mime_type": "image/png"},
    ]
    text = format_media_refs_text(refs)
    assert "[media#1 image/jpeg" in text
    assert "[media#2 image/png" in text
    assert "file_id=AAA" in text
    assert "file_id=BBB" in text


# ---------------------------------------------------------------------------
# _build_telegram_file_url
# ---------------------------------------------------------------------------


def _make_telegram_adapter_with_token(token: str):
    """Build a partially-initialized TelegramAdapter for unit testing helpers.

    We bypass __init__ so we don't need a live bot / event loop.  Only the
    config.token attribute is required for _build_telegram_file_url.
    """
    from gateway.platforms.telegram import TelegramAdapter

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = MagicMock()
    adapter.config.token = token
    return adapter


def test_build_telegram_file_url_uses_cloud_api_pattern():
    adapter = _make_telegram_adapter_with_token("12345:ABC-test-token")
    url = adapter._build_telegram_file_url("photos/file_3.jpg")
    assert url == "https://api.telegram.org/file/bot12345:ABC-test-token/photos/file_3.jpg"


def test_build_telegram_file_url_returns_none_when_path_missing():
    adapter = _make_telegram_adapter_with_token("12345:ABC")
    assert adapter._build_telegram_file_url(None) is None
    assert adapter._build_telegram_file_url("") is None


def test_build_telegram_file_url_passthrough_for_absolute_url():
    """Custom base_file_url (local Bot API server) yields a full URL already."""
    adapter = _make_telegram_adapter_with_token("12345:ABC")
    custom = "https://my-local-bot-api.example/photos/file_3.jpg"
    assert adapter._build_telegram_file_url(custom) == custom


def test_build_telegram_file_url_returns_none_without_token():
    adapter = _make_telegram_adapter_with_token(None)
    assert adapter._build_telegram_file_url("photos/file_3.jpg") is None


# ---------------------------------------------------------------------------
# media_refs propagation through photo batch merge
# ---------------------------------------------------------------------------


def test_enqueue_photo_event_merges_media_refs_across_burst():
    """Multi-photo bursts must preserve one media_ref per photo."""
    from gateway.platforms.telegram import TelegramAdapter
    import asyncio

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    # We avoid scheduling the real flush task because there's no running loop;
    # patch the asyncio.create_task call by giving the adapter a no-op stub.
    adapter._flush_photo_batch = MagicMock(return_value=asyncio.sleep(0))

    source = SessionSource(
        platform="telegram",
        chat_id="55",
        chat_type="dm",
        user_id="user-1",
    )

    event_a = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/a.jpg"],
        media_types=["image/jpeg"],
        media_refs=[{"file_id": "A", "file_url": "uA", "file_path": "/tmp/a.jpg", "mime_type": "image/jpeg"}],
    )
    event_b = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/b.jpg"],
        media_types=["image/jpeg"],
        media_refs=[{"file_id": "B", "file_url": "uB", "file_path": "/tmp/b.jpg", "mime_type": "image/jpeg"}],
    )

    # Need a running loop because the adapter schedules asyncio.create_task.
    async def _run():
        adapter._enqueue_photo_event("k1", event_a)
        adapter._enqueue_photo_event("k1", event_b)

    asyncio.get_event_loop().run_until_complete(_run())

    merged = adapter._pending_photo_batches["k1"]
    assert merged.media_urls == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert [r["file_id"] for r in merged.media_refs] == ["A", "B"]


# ---------------------------------------------------------------------------
# Text-context injection in the main turn flow
# ---------------------------------------------------------------------------


def test_message_event_default_media_refs_is_empty_list():
    """Ensure dataclass default factory produces a fresh list per event."""
    e1 = MessageEvent(text="hi")
    e2 = MessageEvent(text="hello")
    assert e1.media_refs == []
    assert e2.media_refs == []
    e1.media_refs.append({"file_id": "X"})
    # mutation on e1 must not leak to e2 (no shared default mutable)
    assert e2.media_refs == []


def test_format_media_refs_text_for_screenshot_as_document():
    """Telegram delivers screenshots as documents — same ref shape applies."""
    refs = [
        {
            "file_id": "BQACAgI-screenshot-id",
            "file_unique_id": "AQADXxx",
            "file_url": "https://api.telegram.org/file/bot12345:ABC/documents/file_5.png",
            "file_path": "/tmp/cache/img_xyz.png",
            "mime_type": "image/png",
        }
    ]
    text = format_media_refs_text(refs)
    assert "image/png" in text
    assert "file_id=BQACAgI-screenshot-id" in text
    assert "file_url=https://api.telegram.org/file/bot12345:ABC/documents/file_5.png" in text


# ---------------------------------------------------------------------------
# Queued event placeholder includes media_refs (run.py _build_media_placeholder)
# ---------------------------------------------------------------------------


def test_build_media_placeholder_includes_media_refs_block():
    """When a photo event is queued (no caption), the placeholder text
    surfaced to the agent must include the structured media_refs block so
    Bobby can quote the file_id / getFile URL into a PA observation payload.
    """
    from gateway.run import _build_media_placeholder

    event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        media_urls=["/tmp/cache/img_abc.jpg"],
        media_types=["image/jpeg"],
        media_refs=[{
            "file_id": "AgACAg-queued",
            "file_unique_id": "AQADtest",
            "file_url": "https://api.telegram.org/file/bot999:ABC/photos/file_99.jpg",
            "file_path": "/tmp/cache/img_abc.jpg",
            "mime_type": "image/jpeg",
        }],
    )
    placeholder = _build_media_placeholder(event)
    # Original placeholder line still present for backward compatibility
    assert "[User sent an image: /tmp/cache/img_abc.jpg]" in placeholder
    # New: media_refs structured block also present
    assert "file_id=AgACAg-queued" in placeholder
    assert "file_url=https://api.telegram.org/file/bot999:ABC/photos/file_99.jpg" in placeholder


def test_build_media_placeholder_no_refs_falls_back_to_legacy_format():
    """Events without media_refs (e.g. non-Telegram platforms) must still
    produce the legacy placeholder text."""
    from gateway.run import _build_media_placeholder

    event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        media_urls=["/tmp/x.jpg"],
        media_types=["image/jpeg"],
        # media_refs is the empty default
    )
    placeholder = _build_media_placeholder(event)
    assert placeholder == "[User sent an image: /tmp/x.jpg]"
