"""Tests for gateway/shutdown_flush.py — pending message durability (#72680)."""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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




def test_corrupt_flush_file_does_not_starve_later_files(tmp_path, monkeypatch, caplog):
    """A single corrupt flush file must not abort the recovery pass.

    Regression: `except BaseException` listed before `except Exception`
    made every ordinary error (corrupt JSON, DB lock, I/O failure) close
    the owned DB and re-raise out of the loop — and gateway/run.py
    swallows that escape silently, so all later flush files were starved
    on every subsequent startup.
    """
    import logging

    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    good_a = flush_dir / "a_good.json"
    bad = flush_dir / "b_corrupt.json"
    good_c = flush_dir / "c_good.json"
    good_a.write_text(
        json.dumps(
            {
                "session_key": "k1",
                "data": {"text": "hello", "session_id": "s1"},
                "ts": 1,
            }
        ),
        encoding="utf-8",
    )
    bad.write_text("{not valid json", encoding="utf-8")
    good_c.write_text(
        json.dumps(
            {
                "session_key": "k2",
                "data": {"text": "world", "session_id": "s2"},
                "ts": 2,
            }
        ),
        encoding="utf-8",
    )

    db = MagicMock()
    with caplog.at_level(logging.WARNING):
        recovered = recover_pending_to_db(session_db=db)

    assert recovered == 2
    assert db.append_message.call_count == 2
    # The corrupt file is preserved for a later retry, with a warning.
    assert bad.exists()
    assert any("b_corrupt.json" in rec.message for rec in caplog.records)


def test_base_exception_still_propagates_and_closes_owned_db(
    tmp_path, monkeypatch
):
    """Ctrl-C-style interrupts still abort recovery without stranding a DB."""
    from unittest.mock import MagicMock, patch

    flush_dir = _make_flush_dir(tmp_path)
    monkeypatch.setattr(
        "gateway.shutdown_flush._get_flush_dir", lambda: flush_dir
    )
    (flush_dir / "a.json").write_text(
        json.dumps(
            {
                "session_key": "k",
                "data": {"text": "x", "session_id": "s"},
                "ts": 1,
            }
        ),
        encoding="utf-8",
    )

    db = MagicMock()
    db.append_message.side_effect = KeyboardInterrupt()
    fake_sessiondb = MagicMock(return_value=db)
    with patch("hermes_state.SessionDB", fake_sessiondb):
        with pytest.raises(KeyboardInterrupt):
            recover_pending_to_db()
    db.close.assert_called_once()
