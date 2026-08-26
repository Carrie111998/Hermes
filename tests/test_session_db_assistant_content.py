"""Tests for conditional recovery of empty assistant rows."""

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def test_ensure_assistant_message_content_is_conditional(db):
    """Only an active blank assistant row may be filled, and only once."""
    db.create_session("fill-assistant", source="tui")
    assistant_id = db.append_message("fill-assistant", role="assistant", content="")
    user_id = db.append_message("fill-assistant", role="user", content="prompt")

    assert db.ensure_assistant_message_content(
        "fill-assistant", assistant_id, "first answer"
    ) == "first answer"
    assert db.ensure_assistant_message_content(
        "fill-assistant", assistant_id, "competing answer"
    ) == "first answer"
    assert db.ensure_assistant_message_content(
        "fill-assistant", user_id, "not an assistant"
    ) is None
    assert db.ensure_assistant_message_content(
        "fill-assistant", 999999, "missing row"
    ) is None

    messages = db.get_messages_as_conversation("fill-assistant")
    assert messages[0]["content"] == "first answer"
    assert messages[1]["content"] == "prompt"
