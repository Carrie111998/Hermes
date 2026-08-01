"""Regression coverage for rejected foreground MEDIA delivery."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class _MediaFeedbackAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sent_text: list[str] = []
        self.sent_documents: list[str] = []
        self.sent_voices: list[str] = []
        self.sent_videos: list[str] = []
        self.sent_images: list[list[tuple[str, str]]] = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content=None, **kwargs):
        if content:
            self.sent_text.append(content)
        return SendResult(success=True, message_id=f"text-{len(self.sent_text)}")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}

    async def send_document(self, chat_id, file_path, **kwargs):
        self.sent_documents.append(file_path)
        return SendResult(success=True, message_id="document")

    async def send_voice(self, chat_id, audio_path, **kwargs):
        self.sent_voices.append(audio_path)
        return SendResult(success=True, message_id="voice")

    async def send_video(self, chat_id, video_path, **kwargs):
        self.sent_videos.append(video_path)
        return SendResult(success=True, message_id="video")

    async def send_multiple_images(self, chat_id, images, **kwargs):
        self.sent_images.append(images)
        return SendResult(success=True, message_id="images")


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="user-1",
        chat_type="dm",
    )


def _event(text="request", *, metadata=None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="message-1",
        channel_prompt="channel context",
        auto_skill="skill-name",
        metadata=metadata or {},
    )


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._sessions = {}
    runner._background_tasks = set()
    runner._thread_metadata_for_source = lambda source, anchor=None: {"thread_id": "thread-1"}
    runner._reply_anchor_for_event = lambda event: None
    return runner


def _media_paths(tmp_path, monkeypatch):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    safe = allowed_root / "safe.pdf"
    safe.write_bytes(b"safe")
    rejected = tmp_path / "container-output" / "report.pdf"
    rejected.parent.mkdir()
    rejected.write_bytes(b"rejected")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (allowed_root,),
    )
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "1")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "0")
    return safe.resolve(), rejected.resolve()


async def _drain_background_tasks(adapter):
    await asyncio.sleep(0)
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_rejected_nonstream_media_reenters_origin_session_and_recovers(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    events = []
    responses = [
        f"Here is the report.\nMEDIA:{rejected}",
        "I could not deliver the report because the host rejected its path.",
    ]

    async def handler(event):
        events.append(event)
        return responses.pop(0)

    adapter._message_handler = handler
    await adapter._process_message_background(_event(), build_session_key(_source()))
    await _drain_background_tasks(adapter)

    assert len(events) == 2
    assert events[1].internal is True
    assert events[1].metadata["media_delivery_feedback"] is True
    assert json.dumps(str(rejected), ensure_ascii=True) in events[1].text
    assert "listed attachment(s) were not sent" in events[1].text.lower()
    assert adapter.sent_text == [
        "Here is the report.",
        "I could not deliver the report because the host rejected its path.",
    ]
    assert adapter.sent_documents == []


@pytest.mark.asyncio
async def test_rejected_streamed_media_queues_same_session_feedback(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    event = _event()

    await GatewayRunner._deliver_media_from_response(
        runner,
        f"Already streamed text\nMEDIA:{rejected}",
        event,
        adapter,
    )

    pending = adapter.get_pending_message(build_session_key(event.source))
    assert adapter.sent_documents == []
    assert pending is not None
    assert pending.internal is True
    assert pending.metadata["media_delivery_feedback"] is True
    assert "feedback-pending" not in pending.text


@pytest.mark.asyncio
async def test_safe_media_delivery_does_not_queue_feedback(tmp_path, monkeypatch):
    safe, _ = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{safe}")
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    assert adapter.sent_documents == [str(safe)]
    assert adapter._pending_messages == {}
    assert runner._sessions == {}


@pytest.mark.asyncio
async def test_mixed_media_batch_delivers_safe_and_queues_one_feedback(tmp_path, monkeypatch):
    safe, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    events = []

    async def handler(event):
        events.append(event)
        if event.metadata.get("media_delivery_feedback"):
            return "The report could not be recovered."
        return f"Mixed result\nMEDIA:{safe}\nMEDIA:{rejected}"

    adapter._message_handler = handler
    await adapter._process_message_background(_event(), build_session_key(_source()))
    await _drain_background_tasks(adapter)

    assert adapter.sent_text == ["Mixed result", "The report could not be recovered."]
    assert adapter.sent_documents == [str(safe)]
    assert len(events) == 2
    assert adapter._pending_messages == {}
    assert events[1].metadata["media_delivery_feedback"] is True
    assert events[1].text.count(json.dumps(str(rejected), ensure_ascii=True)) == 1


def test_feedback_preserves_existing_fifo_order(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    event = _event()
    session_key = build_session_key(event.source)
    user_event = _event("follow-up")

    runner._enqueue_fifo(session_key, user_event, adapter)
    runner._queue_media_delivery_feedback(event, session_key, adapter, [(str(rejected), False)])

    assert adapter._pending_messages[session_key] is user_event
    queued = runner._sessions[session_key].conversation.queued_events
    assert len(queued) == 1
    assert queued[0].metadata["media_delivery_feedback"] is True
    assert queued[0].source is event.source


@pytest.mark.asyncio
async def test_feedback_turn_rejection_does_not_requeue(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    feedback_event = _event(
        metadata={"media_delivery_feedback": True},
    )
    adapter._message_handler = AsyncMock(
        return_value=f"Retry failed\nMEDIA:{rejected}",
    )

    await adapter._process_message_background(
        feedback_event,
        build_session_key(feedback_event.source),
    )

    assert adapter.sent_text == ["Retry failed"]
    assert adapter.sent_documents == []
    assert adapter._pending_messages == {}
    assert runner._sessions == {}


@pytest.mark.asyncio
async def test_post_validation_upload_failure_keeps_existing_user_notice(tmp_path, monkeypatch):
    safe, _ = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    adapter.send_document = AsyncMock(
        return_value=SendResult(success=False, error="upload failed"),
    )
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{safe}")

    await adapter._process_message_background(_event(), build_session_key(_source()))

    adapter.send_document.assert_awaited_once()
    assert adapter.sent_text == ["⚠️ Couldn't deliver the file attachment (safe.pdf)."]
    assert adapter._pending_messages == {}
    assert runner._sessions == {}


@pytest.mark.asyncio
async def test_adapter_without_runner_preserves_filtering_and_delivery(tmp_path, monkeypatch):
    safe, rejected = _media_paths(tmp_path, monkeypatch)
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = None
    adapter._message_handler = AsyncMock(return_value=f"Safe\nMEDIA:{safe}\nMEDIA:{rejected}")

    await adapter._process_message_background(_event(), build_session_key(_source()))

    assert adapter.sent_text == ["Safe"]
    assert adapter.sent_documents == [str(safe)]
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_internal_media_response_keeps_non_foreground_boundary(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    event = _event(metadata={"internal_source": "watch"})
    event.internal = True
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{rejected}")

    await adapter._process_message_background(event, build_session_key(event.source))
    await _drain_background_tasks(adapter)

    assert adapter._message_handler.await_count == 1
    assert adapter._pending_messages == {}
    assert runner._sessions == {}


@pytest.mark.asyncio
async def test_user_followup_after_feedback_keeps_turn_boundary(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    runner._adapter_for_source = lambda source: adapter
    session_key = build_session_key(_source())
    primary_event = _event()
    runner._queue_media_delivery_feedback(
        primary_event,
        session_key,
        adapter,
        [(str(rejected), False)],
    )
    adapter._busy_text_mode = "queue"

    await adapter._queue_text_debounce(session_key, _event("follow-up"))
    await adapter._flush_text_debounce_now(session_key)

    feedback_event = adapter._pending_messages[session_key]
    assert feedback_event.metadata["media_delivery_feedback"] is True
    queued = runner._sessions[session_key].conversation.queued_events
    assert len(queued) == 1
    assert queued[0].text == "follow-up"


@pytest.mark.asyncio
async def test_feedback_head_keeps_photo_followup_out_of_feedback_turn(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    runner._adapter_for_source = lambda source: adapter
    session_key = build_session_key(_source())
    runner._queue_media_delivery_feedback(
        _event(),
        session_key,
        adapter,
        [(str(rejected), False)],
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._busy_session_handler = AsyncMock(return_value=False)
    adapter._message_handler = AsyncMock()
    photo = _event("photo follow-up")
    photo.message_type = MessageType.PHOTO

    await adapter.handle_message(photo)

    feedback_event = adapter._pending_messages[session_key]
    assert feedback_event.metadata["media_delivery_feedback"] is True
    queued = runner._sessions[session_key].conversation.queued_events
    assert len(queued) == 1
    assert queued[0] is photo


@pytest.mark.asyncio
async def test_session_command_drops_stale_feedback_and_keeps_followup(tmp_path, monkeypatch):
    _, rejected = _media_paths(tmp_path, monkeypatch)
    runner = _runner()
    adapter = _MediaFeedbackAdapter()
    adapter.gateway_runner = runner
    session_key = build_session_key(_source())
    command_guard = asyncio.Event()
    adapter._active_sessions[session_key] = command_guard

    runner._queue_media_delivery_feedback(
        _event(),
        session_key,
        adapter,
        [(str(rejected), False)],
    )
    followup = _event("follow-up")
    runner._enqueue_fifo(session_key, followup, adapter)

    started = []
    adapter._start_session_processing = lambda event, key: started.append((event, key)) or True

    await adapter._drain_pending_after_session_command(session_key, command_guard)

    assert started == [(followup, session_key)]
    assert adapter._active_sessions == {}
