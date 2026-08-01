import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.inflight_journal as journal
from gateway.config import Platform, PlatformConfig
from gateway.inflight_journal import claim_inflight, clear_inflight, list_inflight
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from plugins.platforms.telegram.adapter import TelegramAdapter


def _event(text="private raw text must not be stored"):
    return MessageEvent(
        text=text,
        message_type=MessageType.VOICE,
        message_id="88",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="429731663",
            thread_id="3",
            message_id="88",
        ),
    )


def _adapter():
    return _StubAdapter(
        PlatformConfig(
            enabled=True,
            token="t",
            extra={"inflight_recovery_enabled": True},
        ),
        Platform.TELEGRAM,
    )


def test_journal_persists_only_routing_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = claim_inflight(_event())
    record = list_inflight("telegram")[0]
    assert record["chat_id"] == "-1001"
    assert record["thread_id"] == "3"
    assert record["message_id"] == "88"
    assert record["message_type"] == "voice"
    assert isinstance(record["process_id"], int)
    serialized = json.dumps(record, ensure_ascii=False)
    assert "private raw text" not in serialized
    assert "429731663" not in serialized
    assert clear_inflight(token) is True
    assert list_inflight("telegram") == []


def test_clear_failure_is_reported_and_path_is_retained(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = claim_inflight(_event())
    original_unlink = Path.unlink

    def fail_target(self, *args, **kwargs):
        if str(self) == token:
            raise PermissionError("blocked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    assert clear_inflight(token) is False
    assert Path(token).exists()


def test_clear_rejects_path_outside_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    assert clear_inflight(outside) is False
    assert outside.read_text() == "keep"


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SimpleNamespace(success=True)

    async def send_document(self, chat_id, file_path, **kwargs):
        return getattr(self, "document_result", SimpleNamespace(success=True, error=None))

    async def get_chat_info(self, chat_id):
        return {}


async def _run_background(adapter, event):
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    await adapter._process_message_background(event, session_key)


@pytest.mark.asyncio
async def test_normal_background_delivery_clears_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    adapter.send_typing = AsyncMock(return_value=None)
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True))
    adapter._message_handler = AsyncMock(return_value="ok")
    await _run_background(adapter, _event("hello"))
    assert list_inflight("telegram") == []


@pytest.mark.asyncio
async def test_failed_background_delivery_retains_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    adapter.send_typing = AsyncMock(return_value=None)
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=False))
    adapter._message_handler = AsyncMock(return_value="ok")
    await _run_background(adapter, _event("hello"))
    assert len(list_inflight("telegram")) == 1


@pytest.mark.asyncio
async def test_mixed_text_and_failed_attachment_retains_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    media = tmp_path / "result.txt"
    media.write_text("artifact")
    adapter = _adapter()
    adapter.send_typing = AsyncMock(return_value=None)
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True))
    adapter.document_result = SimpleNamespace(success=False, error="upload failed")
    adapter._message_handler = AsyncMock(return_value=f"done\nMEDIA:{media}")
    await _run_background(adapter, _event("hello"))
    assert len(list_inflight("telegram")) == 1


@pytest.mark.asyncio
async def test_attachment_only_success_clears_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    media = tmp_path / "result.txt"
    media.write_text("artifact")
    adapter = _adapter()
    adapter.send_typing = AsyncMock(return_value=None)
    adapter._message_handler = AsyncMock(return_value=f"MEDIA:{media}")
    await _run_background(adapter, _event("hello"))
    assert list_inflight("telegram") == []


@pytest.mark.asyncio
async def test_busy_queued_messages_are_journaled_before_background(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    adapter._message_handler = AsyncMock(return_value="ok")
    first = _event("one")
    first.message_type = MessageType.PHOTO
    first.message_id = "101"
    second = _event("two")
    second.message_type = MessageType.PHOTO
    second.message_id = "102"
    session_key = build_session_key(first.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._session_tasks[session_key] = asyncio.current_task()
    try:
        await adapter.handle_message(first)
        await adapter.handle_message(second)
        assert len(list_inflight("telegram")) == 2
        pending = adapter._pending_messages[session_key]
        assert len(getattr(pending, "_gateway_inflight_tokens")) == 2
    finally:
        adapter._pending_messages.clear()
        adapter._active_sessions.clear()
        adapter._session_tasks.clear()


@pytest.mark.asyncio
async def test_production_busy_disposition_keeps_queued_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    adapter.set_message_handler(AsyncMock(return_value="done"))

    async def _busy(event, _session_key):
        setattr(event, "_gateway_busy_disposition", "queued")
        return True

    adapter.set_busy_session_handler(_busy)
    event = _event()
    event.message_id = "queued-production"
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    current = asyncio.current_task()
    assert current is not None
    adapter._session_tasks[session_key] = current
    try:
        await adapter.handle_message(event)
        assert len(list_inflight("telegram")) == 1
    finally:
        adapter._session_tasks.pop(session_key, None)
        adapter._active_sessions.pop(session_key, None)


@pytest.mark.asyncio
async def test_steered_marker_clears_with_active_turn_final_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    adapter.set_message_handler(AsyncMock(return_value="done"))

    async def _busy(event, _session_key):
        setattr(event, "_gateway_busy_disposition", "active_turn")
        return True

    adapter.set_busy_session_handler(_busy)
    event = _event()
    event.message_id = "steered-production"
    session_key = build_session_key(event.source)
    generation = object()
    setattr(adapter, "_gateway_inflight_turn_generations", {session_key: generation})
    adapter._active_sessions[session_key] = asyncio.Event()
    current = asyncio.current_task()
    assert current is not None
    adapter._session_tasks[session_key] = current
    try:
        await adapter.handle_message(event)
        assert len(list_inflight("telegram")) == 1
        active_event = _event()
        active_event.message_id = "active-original"
        adapter._clear_event_inflight(active_event, session_key, generation)
        assert list_inflight("telegram") == []
    finally:
        adapter._session_tasks.pop(session_key, None)
        adapter._active_sessions.pop(session_key, None)


@pytest.mark.asyncio
async def test_steer_completion_race_leaves_marker_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    event = _event()
    event.message_id = "steer-race"
    session_key = build_session_key(event.source)
    old_generation = object()
    generation_map = {session_key: old_generation}
    setattr(adapter, "_gateway_inflight_turn_generations", generation_map)

    async def _busy(event_arg, _session_key):
        setattr(event_arg, "_gateway_busy_disposition", "active_turn")
        generation_map[session_key] = object()
        return True

    adapter.set_message_handler(AsyncMock(return_value="done"))
    adapter.set_busy_session_handler(_busy)
    adapter._active_sessions[session_key] = asyncio.Event()
    current = asyncio.current_task()
    assert current is not None
    adapter._session_tasks[session_key] = current
    try:
        await adapter.handle_message(event)
        assert len(list_inflight("telegram")) == 1
        active = getattr(adapter, "_gateway_active_inflight_tokens", {})
        assert (session_key, old_generation) not in active
    finally:
        adapter._session_tasks.pop(session_key, None)
        adapter._active_sessions.pop(session_key, None)


@pytest.mark.asyncio
async def test_failed_active_turn_marker_is_not_cleared_by_next_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter()
    session_key = build_session_key(_event().source)

    async def failed_handler(_event_arg):
        generation_map = getattr(adapter, "_gateway_inflight_turn_generations")
        generation = generation_map[session_key]
        steered = _event()
        steered.message_id = "steered-failed"
        adapter._claim_event_inflight(steered)
        adapter._transfer_event_inflight_to_active_turn(
            steered, session_key, generation
        )
        return "not delivered"

    failed_turn = _event()
    failed_turn.message_id = "failed-active"
    adapter.send_typing = AsyncMock(return_value=None)
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=False))
    adapter._message_handler = failed_handler
    await adapter._process_message_background(failed_turn, session_key)
    assert len(list_inflight("telegram")) == 2

    next_turn = _event()
    next_turn.message_id = "next-success"
    adapter._send_with_retry = AsyncMock(return_value=SimpleNamespace(success=True))
    adapter._message_handler = AsyncMock(return_value="delivered")
    await adapter._process_message_background(next_turn, session_key)
    records = list_inflight("telegram")
    assert len(records) == 2
    assert {record["message_id"] for record in records} == {
        "steered-failed",
        "failed-active",
    }


@pytest.mark.asyncio
async def test_telegram_startup_reconciliation_deduplicates_topic_notices(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(journal.os, "getpid", lambda: 111)
    first = _event()
    claim_inflight(first)
    second = _event()
    second.message_id = "89"
    claim_inflight(second)
    monkeypatch.setattr(journal.os, "getpid", lambda: 222)
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="t", extra={"inflight_recovery_enabled": True})
    )
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    await adapter._reconcile_inflight_journal()
    adapter.send.assert_awaited_once()
    kwargs = adapter.send.await_args.kwargs
    assert kwargs["chat_id"] == "-1001"
    assert kwargs["metadata"]["message_thread_id"] == "3"
    assert "прервана" in kwargs["content"]
    assert list_inflight("telegram") == []


@pytest.mark.asyncio
async def test_telegram_failed_recovery_notice_remains_for_next_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(journal.os, "getpid", lambda: 111)
    claim_inflight(_event())
    monkeypatch.setattr(journal.os, "getpid", lambda: 222)
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="t", extra={"inflight_recovery_enabled": True})
    )
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=False))
    await adapter._reconcile_inflight_journal()
    assert len(list_inflight("telegram")) == 1


@pytest.mark.asyncio
async def test_reconciliation_skips_marker_claimed_by_current_process(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(journal.os, "getpid", lambda: 333)
    claim_inflight(_event())
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="t", extra={"inflight_recovery_enabled": True})
    )
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    await adapter._reconcile_inflight_journal()
    adapter.send.assert_not_awaited()
    assert len(list_inflight("telegram")) == 1


def test_cold_start_reconciles_before_polling_accepts_updates():
    source = inspect.getsource(TelegramAdapter.connect)
    reconcile_at = source.index("self._reconcile_inflight_journal()")
    polling_at = source.index("self._start_polling_resilient(")
    assert reconcile_at < polling_at
