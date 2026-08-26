"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.shutdown_flush as shutdown_flush
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.shutdown_flush import (
    _serialise_value,
    flush_pending_to_file,
    recover_pending_to_db,
)


def _make_flush_dir(tmp_path: Path) -> Path:
    """Create a temp flush dir and monkeypatch _get_flush_dir to use it."""
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True)
    return flush_dir


def test_flush_writes_string_pending_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    pending = {"agent:main:telegram:supergroup:123": "hello world"}
    count = flush_pending_to_file(pending, reason="shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["session_key"] == "agent:main:telegram:supergroup:123"
    assert payload["reason"] == "shutdown"
    assert payload["data"]["text"] == "hello world"
    assert ":" not in files[0].name
    assert "telegram" not in files[0].name


def test_flush_writes_message_event_to_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MagicMock()
    event.text = "user message"
    event.session_id = "20260728_120000_abc"
    event.platform = "telegram"
    event.sender_id = "456"
    event.sender_name = "Alice"
    event.reply_to = None
    event.media = None
    event.raw_event = None

    count = flush_pending_to_file({"session_key_1": event}, reason="adapter_shutdown")
    assert count == 1
    files = list(flush_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["data"]["text"] == "user message"
    assert payload["data"]["session_id"] == "20260728_120000_abc"


@pytest.mark.asyncio
async def test_recover_reinjects_internal_event_and_deletes_file(
    tmp_path, monkeypatch
):
    """A queued synthetic wake must resume as an event, not inert transcript text."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="owner-1",
        thread_id="topic-7",
        profile="operator",
    )
    event = MessageEvent(
        text="[kanban] Task t_test completed.",
        message_type=MessageType.TEXT,
        source=source,
        message_id="wake-1",
        internal=True,
        metadata={
            "gateway_session_key": "agent:operator:telegram:dm:chat-1:topic-7",
            "kanban_task_id": "t_test",
        },
        allow_gateway_control=False,
    )
    assert flush_pending_to_file({"session-key": event}, reason="shutdown") == 1

    adapter = MagicMock()
    adapter.handle_message = AsyncMock()
    replayed, remaining = await shutdown_flush.recover_pending_internal_events(
        {Platform.TELEGRAM: adapter}
    )

    assert (replayed, remaining) == (1, 0)
    adapter.handle_message.assert_awaited_once()
    await_args = adapter.handle_message.await_args
    assert await_args is not None
    recovered = await_args.args[0]
    assert recovered.text == event.text
    assert recovered.message_type is MessageType.TEXT
    assert recovered.internal is True
    assert recovered.allow_gateway_control is False
    assert recovered.message_id == "wake-1"
    assert recovered.source == source
    assert recovered.metadata == event.metadata
    assert list(flush_dir.glob("*.json")) == []


@pytest.mark.asyncio
async def test_recover_keeps_internal_event_when_adapter_rejects_it(
    tmp_path, monkeypatch
):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MessageEvent(
        text="[kanban] Task t_retry completed.",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1"),
        internal=True,
        allow_gateway_control=False,
    )
    assert flush_pending_to_file({"session-key": event}, reason="shutdown") == 1

    adapter = MagicMock()
    adapter.handle_message = AsyncMock(side_effect=RuntimeError("not ready"))
    replayed, remaining = await shutdown_flush.recover_pending_internal_events(
        {Platform.TELEGRAM: adapter}
    )

    assert (replayed, remaining) == (0, 1)
    assert len(list(flush_dir.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_recover_skips_malformed_entry_and_replays_later_event(
    tmp_path, monkeypatch
):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    (flush_dir / "000-poison.json").write_text(
        json.dumps({"data": ["not", "an", "object"]}),
        encoding="utf-8",
    )
    event = MessageEvent(
        text="[kanban] Task t_after_poison completed.",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1"),
        internal=True,
        allow_gateway_control=False,
    )
    assert flush_pending_to_file({"session-key": event}, reason="shutdown") == 1

    adapter = MagicMock()
    adapter.handle_message = AsyncMock()
    replayed, remaining = await shutdown_flush.recover_pending_internal_events(
        {Platform.TELEGRAM: adapter}
    )

    assert (replayed, remaining) == (1, 1)
    adapter.handle_message.assert_awaited_once()
    assert (flush_dir / "000-poison.json").exists()


def test_transcript_recovery_does_not_consume_internal_event(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    event = MessageEvent(
        text="[kanban] Task t_pending completed.",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1"),
        internal=True,
        allow_gateway_control=False,
    )
    assert flush_pending_to_file({"session-key": event}, reason="shutdown") == 1

    mock_db = MagicMock()
    assert recover_pending_to_db(mock_db) == 0
    mock_db.append_message.assert_not_called()
    assert len(list(flush_dir.glob("*.json"))) == 1


def test_recover_inserts_via_append_message_and_deletes_file(tmp_path, monkeypatch):
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    ts = int(time.time())
    # Write a flush file with session_id
    payload = {
        "session_key": "agent:main:telegram:supergroup:123",
        "reason": "shutdown",
        "ts": ts,
        "data": {
            "text": "lost message",
            "session_id": "20260728_120000_abc",
        },
    }
    flush_file = flush_dir / "test_session_123.json"
    flush_file.write_text(json.dumps(payload), encoding="utf-8")

    mock_db = MagicMock()
    count = recover_pending_to_db(mock_db)

    assert count == 1
    mock_db.append_message.assert_called_once_with(
        session_id="20260728_120000_abc",
        role="user",
        content="lost message",
        timestamp=ts,
    )
    assert not flush_file.exists()


def test_recover_closes_owned_db_when_unexpected_exception_escapes(
    tmp_path, monkeypatch
):
    """Owned SessionDB must close even when recovery is interrupted."""
    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    (flush_dir / "pending.json").write_text(
        json.dumps(
            {
                "session_key": "agent:main:telegram:123",
                "data": {"text": "message", "session_id": "sid"},
            }
        ),
        encoding="utf-8",
    )

    class InterruptingDB:
        closed = False

        def append_message(self, **_kwargs):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True

    db = InterruptingDB()
    monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

    with pytest.raises(KeyboardInterrupt):
        recover_pending_to_db()

    assert db.closed is True


def test_serialise_object_with_text():
    obj = MagicMock()
    obj.text = "msg"
    obj.session_id = "sid"
    obj.platform = None
    obj.sender_id = None
    obj.sender_name = None
    obj.reply_to = None
    obj.media = None
    obj.raw_event = None
    result = _serialise_value(obj)
    assert result is not None
    assert result["text"] == "msg"
    assert result["session_id"] == "sid"


def test_get_flush_dir_uses_get_hermes_home(tmp_path, monkeypatch):
    """Flush dir must use get_hermes_home(), not hardcoded Path.home()."""
    import gateway.shutdown_flush as mod

    captured = {}

    def fake_get_hermes_home():
        from pathlib import Path
        captured["called"] = True
        return tmp_path

    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", fake_get_hermes_home
    )
    result = mod._get_flush_dir()
    assert captured.get("called") is True
    assert result == tmp_path / "pending_messages"


