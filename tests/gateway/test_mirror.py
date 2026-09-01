"""Tests for gateway/mirror.py — session mirroring."""

import json
from unittest.mock import patch, MagicMock

import gateway.mirror as mirror_mod
from gateway.mirror import (
    mirror_to_session,
    _find_session_id,
)


def _setup_sessions(tmp_path, sessions_data):
    """Helper to write a fake sessions.json and patch module-level paths."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "sessions.json"
    index_file.write_text(json.dumps(sessions_data))
    return sessions_dir, index_file


class TestFindSessionId:
    def test_finds_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "agent:main:telegram:dm": {
                "session_id": "sess_abc",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_abc"

    def test_returns_most_recent(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "old": {
                "session_id": "sess_old",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "new": {
                "session_id": "sess_new",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_new"

    def test_thread_id_disambiguates_same_chat(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "topic_a": {
                "session_id": "sess_topic_a",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "10"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "topic_b": {
                "session_id": "sess_topic_b",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "11"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001", thread_id="10")

        assert result == "sess_topic_a"


class TestMirrorToSession:


    def test_successful_mirror_uses_user_id_for_group_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session(
                "telegram",
                "-1001",
                "Hello group!",
                source_label="cli",
                user_id="alice",
            )

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_alice"

    def test_no_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = mirror_to_session("telegram", "99999", "Hello!")

        assert result is False


class TestAppendToSqlite:
    def test_connection_is_closed_after_use(self, tmp_path):
        """Verify _append_to_sqlite closes the SessionDB connection."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"id": "sess_1", "ended_at": None}

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.append_message.assert_called_once()
        mock_db.close.assert_called_once()

    def test_refuses_to_write_into_ended_session(self):
        """#100177: after a session_reset expiry the routing entry still
        points at the ended session, so cron deliveries / hermes send were
        appended to a dead transcript and lost when the #54878 self-heal
        started a fresh session. The append must be refused, not silent."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        mock_db.get_session.return_value = {
            "id": "sess_dead",
            "ended_at": 1_756_000_000.0,
            "end_reason": "session_reset",
        }

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_dead", {"role": "user", "content": "brief"})

        mock_db.append_message.assert_not_called()
        mock_db.close.assert_called_once()

    def test_writes_when_session_lookup_fails(self):
        """A lookup failure must not block the delivery — a mirror that
        can't verify the session is better than a lost message."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        mock_db.get_session.side_effect = RuntimeError("db busy")

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_1", {"role": "user", "content": "brief"})

        mock_db.append_message.assert_called_once()
        mock_db.close.assert_called_once()

    def test_writes_when_session_row_missing(self):
        """No row (deterministic seed session not yet created) still writes —
        append_message creates the row, which is the seeded-cron path."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        mock_db.get_session.return_value = None

        with patch("hermes_state.SessionDB", return_value=mock_db):
            _append_to_sqlite("sess_new", {"role": "user", "content": "brief"})

        mock_db.append_message.assert_called_once()

