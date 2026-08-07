"""Tests for Telegram text message aggregation.

When a user sends a long message, Telegram clients split it into multiple
updates.  The TelegramAdapter should buffer rapid successive text messages
from the same session and aggregate them before dispatching.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._message_handler = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _make_event(
    text: str,
    chat_id: str = "12345",
    *,
    reply_to_text: str | None = None,
    reply_to_is_own_message: bool = False,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
        reply_to_text=reply_to_text,
        reply_to_is_own_message=reply_to_is_own_message,
    )


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
    async def test_supervisor_gate_flushes_ordinary_batch_without_merging_identity(self):
        adapter = _make_adapter()
        handle_message = AsyncMock()
        adapter.handle_message = handle_message
        ordinary = _make_event("ordinary pending text")
        gate = _make_event(
            "approve release",
            reply_to_text=(
                "Decision needed\n"
                "[kanban-gate:0123456789abcdef0123456789abcdef]"
            ),
            reply_to_is_own_message=True,
        )
        adapter._enqueue_text_event(ordinary)

        await adapter._dispatch_supervisor_gate_event(gate)

        assert handle_message.await_count == 2
        dispatched = [call.args[0] for call in handle_message.await_args_list]
        assert dispatched == [ordinary, gate]
        assert dispatched[0].reply_to_text is None
        assert dispatched[0].reply_to_is_own_message is False
        assert dispatched[1].reply_to_text == gate.reply_to_text
        assert dispatched[1].reply_to_is_own_message is True
        assert adapter._pending_text_batches == {}
        assert adapter._pending_text_batch_tasks == {}

    @pytest.mark.asyncio
    async def test_text_handler_routes_supervisor_gate_around_batching(self):
        adapter = _make_adapter()
        gate = _make_event(
            "approve release",
            reply_to_text="[kanban-gate:0123456789abcdef0123456789abcdef]",
            reply_to_is_own_message=True,
        )
        msg = SimpleNamespace(text="approve release")
        update = SimpleNamespace(effective_message=msg, message=msg, update_id=7)
        adapter._is_user_authorized_from_message = Mock(return_value=True)
        adapter._should_process_message = Mock(return_value=True)
        adapter._ensure_forum_commands = AsyncMock()
        adapter._build_message_event = Mock(return_value=gate)
        adapter._clean_bot_trigger_text = Mock(side_effect=lambda text: text)
        adapter._cache_replied_media = AsyncMock()
        adapter._apply_telegram_group_observe_attribution = Mock(
            side_effect=lambda event: event
        )
        adapter._dispatch_supervisor_gate_event = AsyncMock()
        adapter._enqueue_text_event = Mock()

        await adapter._handle_text_message(update, None)

        adapter._dispatch_supervisor_gate_event.assert_awaited_once_with(gate)
        adapter._enqueue_text_event.assert_not_called()


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
