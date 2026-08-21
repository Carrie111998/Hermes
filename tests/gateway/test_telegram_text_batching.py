"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.session import build_session_key


def _make_adapter():
    """Create a minimal TelegramAdapter for testing text batching."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token")
    adapter = object.__new__(TelegramAdapter)
    adapter._platform = Platform.TELEGRAM
    adapter.platform = Platform.TELEGRAM
    adapter.config = config
    adapter._running = True
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._drop_delayed_deliveries = False
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    adapter._media_group_events = {}
    adapter._media_group_tasks = {}
    adapter._polling_error_task = None
    adapter._polling_heartbeat_task = None
    adapter._app = None
    adapter._bot = None
    adapter._set_status_indicator = AsyncMock()
    adapter._release_platform_lock = lambda: None
    adapter._text_batch_delay_seconds = 0.1  # fast for tests
    adapter._media_batch_delay_seconds = 0.1
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(
    text: str,
    chat_id: str = "12345",
    *,
    message_id: str | None = None,
    update_id: int | None = None,
    thread_id: str | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="dm",
            thread_id=thread_id,
        ),
        raw_message=SimpleNamespace(text=text),
        message_id=message_id,
        platform_update_id=update_id,
        timestamp=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
    )


def _make_typed_event(text: str, update_id: int) -> MessageEvent:
    event = _make_event(text)
    event.metadata["telegram_update"] = {
        "event_type": "message.new",
        "update_id": update_id,
        "message_id": str(update_id),
        "dispatch_kind": "gateway_dispatch",
        "payload_hash": f"payload-{update_id}",
        "content_hash": f"content-{update_id}",
    }
    return event


class TestTextBatching:
    @pytest.mark.asyncio
    async def test_single_message_dispatched_after_delay(self):
        adapter = _make_adapter()
        event = _make_event("hello world")

        adapter._enqueue_text_event(event)

        # Not dispatched yet
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "hello world"

    @pytest.mark.asyncio
    async def test_split_messages_aggregated(self):
        """Two rapid messages from the same chat should be merged."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("This is part one of a long"))
        await asyncio.sleep(0.02)  # small gap, within batch window
        adapter._enqueue_text_event(_make_event("message that was split by Telegram."))

        # Not dispatched yet (timer restarted)
        adapter.handle_message.assert_not_called()

        # Wait for flush
        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        dispatched = adapter.handle_message.call_args[0][0]
        assert "part one" in dispatched.text
        assert "split by Telegram" in dispatched.text

    @pytest.mark.asyncio
    async def test_split_messages_preserve_each_source_update_for_observers(self):
        """Batching must not erase unique message IDs, text, or topic provenance."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(
            _make_event(
                "rough idea", message_id="41", update_id=101, thread_id="topic-7"
            )
        )
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(
            _make_event(
                "later iteration", message_id="42", update_id=102, thread_id="topic-7"
            )
        )

        await asyncio.sleep(0.2)

        dispatched = adapter.handle_message.call_args[0][0]
        assert dispatched.text == "rough idea\nlater iteration"
        assert dispatched.metadata["telegram_source_messages"] == [
            {
                "message_id": "41",
                "platform_update_id": "101",
                "thread_id": "topic-7",
                "source_timestamp": "2026-08-07T12:00:00+00:00",
                "source_text": "rough idea",
                "reply_to_message_id": "",
                "message_type": "text",
            },
            {
                "message_id": "42",
                "platform_update_id": "102",
                "thread_id": "topic-7",
                "source_timestamp": "2026-08-07T12:00:00+00:00",
                "source_text": "later iteration",
                "reply_to_message_id": "",
                "message_type": "text",
            },
        ]

    @pytest.mark.asyncio
    async def test_split_batch_preserves_all_updates_without_claiming_single_identity(self):
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_typed_event("chunk one", 101))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_typed_event("chunk two", 102))
        await asyncio.sleep(0.2)

        dispatched = adapter.handle_message.call_args[0][0]
        assert "telegram_update" not in dispatched.metadata
        assert [
            item["update_id"] for item in dispatched.metadata["telegram_updates"]
        ] == [101, 102]

    @pytest.mark.asyncio
    async def test_photo_batch_preserves_all_updates_without_claiming_single_identity(self):
        adapter = _make_adapter()
        first = _make_typed_event("first caption", 201)
        first.media_urls = ["/tmp/first.jpg"]
        first.media_types = ["image/jpeg"]
        second = _make_typed_event("second caption", 202)
        second.media_urls = ["/tmp/second.jpg"]
        second.media_types = ["image/jpeg"]

        adapter._enqueue_photo_event("album", first)
        await asyncio.sleep(0.02)
        adapter._enqueue_photo_event("album", second)
        await asyncio.sleep(0.2)

        dispatched = adapter.handle_message.call_args[0][0]
        assert "telegram_update" not in dispatched.metadata
        assert [
            item["update_id"] for item in dispatched.metadata["telegram_updates"]
        ] == [201, 202]

    @pytest.mark.asyncio
    async def test_media_group_preserves_all_updates_without_claiming_single_identity(self):
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = _make_adapter()
        first = _make_typed_event("album caption", 301)
        first.media_urls = ["/tmp/first.jpg"]
        first.media_types = ["image/jpeg"]
        second = _make_typed_event("", 302)
        second.media_urls = ["/tmp/second.jpg"]
        second.media_types = ["image/jpeg"]

        with patch.object(TelegramAdapter, "MEDIA_GROUP_WAIT_SECONDS", 0.1):
            await adapter._queue_media_group_event("album", first)
            await asyncio.sleep(0.02)
            await adapter._queue_media_group_event("album", second)
            await asyncio.sleep(0.2)

        dispatched = adapter.handle_message.call_args[0][0]
        assert "telegram_update" not in dispatched.metadata
        assert [
            item["update_id"] for item in dispatched.metadata["telegram_updates"]
        ] == [301, 302]

    @pytest.mark.asyncio
    async def test_three_way_split_aggregated(self):
        """Three rapid messages should all merge."""
        adapter = _make_adapter()

        adapter._enqueue_text_event(_make_event("chunk 1"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 2"))
        await asyncio.sleep(0.02)
        adapter._enqueue_text_event(_make_event("chunk 3"))

        await asyncio.sleep(0.2)

        adapter.handle_message.assert_called_once()
        text = adapter.handle_message.call_args[0][0].text
        assert "chunk 1" in text
        assert "chunk 2" in text
        assert "chunk 3" in text


    @pytest.mark.asyncio
    async def test_disconnected_adapter_drops_pending_media_group_flush_before_dispatch(self):
        """A pending media group should not dispatch after disconnect starts."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = _make_adapter()
        event = _make_event("album caption")
        event.media_urls = ["/tmp/photo.jpg"]
        event.media_types = ["image/jpeg"]

        with patch.object(TelegramAdapter, "MEDIA_GROUP_WAIT_SECONDS", 0.1):
            await adapter._queue_media_group_event("album-1", event)
            adapter._mark_disconnected()
            await asyncio.sleep(0.2)

        adapter.handle_message.assert_not_called()
        assert adapter._media_group_events == {}
        assert adapter._media_group_tasks == {}


    @pytest.mark.asyncio
    async def test_disconnect_cancels_all_pending_delivery_task_maps(self):
        """Photo/media/polling delayed tasks are awaited and queues are cleared."""
        adapter = _make_adapter()
        tasks = [asyncio.create_task(asyncio.sleep(0.2)) for _ in range(4)]
        adapter._pending_text_batches["text"] = _make_event("text")
        adapter._pending_text_batch_tasks["text"] = tasks[0]
        adapter._pending_photo_batches["photo"] = _make_event("photo")
        adapter._pending_photo_batch_tasks["photo"] = tasks[1]
        adapter._media_group_events["media"] = _make_event("media")
        adapter._media_group_tasks["media"] = tasks[2]
        adapter._polling_error_task = tasks[3]

        await adapter.disconnect()

        assert all(task.done() for task in tasks)
        assert adapter._pending_text_batches == {}
        assert adapter._pending_text_batch_tasks == {}
        assert adapter._pending_photo_batches == {}
        assert adapter._pending_photo_batch_tasks == {}
        assert adapter._media_group_events == {}
        assert adapter._media_group_tasks == {}
        assert adapter._polling_error_task is None
