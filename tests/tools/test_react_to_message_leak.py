"""Tests for react_to_message_tool SessionDB leak fix.

Verifies that the dedicated SessionDB handle is always closed on every return
path (success and all error paths) — the regression this guards is the
original leak where no return path called ``db.close()``.
"""

from types import SimpleNamespace
from unittest.mock import patch

import tools.react_to_message_tool as rtm


def _fake_db():
    """A fake SessionDB that records whether close() was called."""
    db = SimpleNamespace(
        latest_message_row_id=lambda *a, **k: 5,
        get_message_role=lambda *a, **k: "user",
        set_message_reaction=lambda *a, **k: ["❤️"],
        close_called=False,
    )
    db.close = lambda: setattr(db, "close_called", True)
    return db


def _call(emoji="❤️", **kwargs):
    session_key = "test-session"
    with patch.object(rtm, "get_session_env", return_value=session_key):
        with patch.object(rtm, "_open_session_db") as mock_open:
            db = _fake_db()
            mock_open.return_value = db
            result = rtm.react_to_message_tool(emoji, **kwargs)
            return result, db


def test_success_path_closes_db():
    result, db = _call()
    assert "success" in result
    assert db.close_called, "db.close() must be called on success path"


def test_no_user_message_path_closes_db():
    def fake_none(*a, **k):
        return None

    session_key = "test-session"
    with patch.object(rtm, "get_session_env", return_value=session_key):
        with patch.object(rtm, "_open_session_db") as mock_open:
            db = SimpleNamespace(
                latest_message_row_id=fake_none,
                close_called=False,
            )
            db.close = lambda: setattr(db, "close_called", True)
            mock_open.return_value = db
            result = rtm.react_to_message_tool("❤️")
            assert "error" in result
            assert db.close_called, "db.close() must be called when no user message found"


def test_db_open_failure_no_close_needed():
    session_key = "test-session"
    with patch.object(rtm, "get_session_env", return_value=session_key):
        with patch.object(rtm, "_open_session_db", return_value=None):
            result = rtm.react_to_message_tool("❤️")
            assert "error" in result
