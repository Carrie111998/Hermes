"""Regression coverage for persisted empty-response scaffolding repair."""

import pytest

from agent.conversation_loop import _EMPTY_TOOL_RESPONSE_NUDGE
from hermes_state import (
    _PERSISTED_EMPTY_RECOVERY_NUDGE,
    SessionDB,
)


_EMPTY_RECOVERY_NUDGE = (
    "You just executed tool calls but returned an empty response. "
    "Please process the tool results above and continue with the task."
)


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _seed_polluted_session(db, session_id="s1"):
    db.create_session(session_id, "system prompt")
    db.append_message(session_id, role="user", content="run the task")
    db.append_message(
        session_id,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
        ],
    )
    db.append_message(
        session_id,
        role="tool",
        content="result",
        tool_call_id="call_1",
    )
    # Private metadata flags are absent after durable projection.
    db.append_message(session_id, role="assistant", content="(empty)")
    db.append_message(session_id, role="user", content=_EMPTY_RECOVERY_NUDGE)
    db.append_message(session_id, role="assistant", content="Recovered answer.")


def test_repair_signature_matches_live_empty_recovery_nudge():
    assert _PERSISTED_EMPTY_RECOVERY_NUDGE == _EMPTY_TOOL_RESPONSE_NUDGE


def test_resume_strips_persisted_empty_recovery_scaffolding(db):
    _seed_polluted_session(db)

    messages = db.get_messages_as_conversation("s1", repair_alternation=True)

    assert [message["content"] for message in messages] == [
        "run the task",
        "",
        "result",
        "Recovered answer.",
    ]


def test_resume_views_both_hide_persisted_empty_recovery_scaffolding(db):
    _seed_polluted_session(db)

    model_history, display_history = db.get_resume_conversations("s1")

    for history in (model_history, display_history):
        contents = [message["content"] for message in history]
        assert "(empty)" not in contents
        assert _EMPTY_RECOVERY_NUDGE not in contents
        assert "Recovered answer." in contents


def test_similar_real_content_is_preserved(db):
    db.create_session("clean", "system prompt")
    db.append_message("clean", role="user", content="Explain an empty response")
    db.append_message(
        "clean",
        role="assistant",
        content="The set is (empty), but this is a real explanation.",
    )
    db.append_message(
        "clean",
        role="user",
        content=_EMPTY_RECOVERY_NUDGE + " Please include details.",
    )
    db.append_message("clean", role="assistant", content="(empty)")
    db.append_message("clean", role="user", content="Continue the real conversation.")
    db.append_message("clean", role="assistant", content="Still part of the answer.")
    db.append_message("clean", role="user", content=_EMPTY_RECOVERY_NUDGE)

    model_history, display_history = db.get_resume_conversations("clean")

    expected = [
        "Explain an empty response",
        "The set is (empty), but this is a real explanation.",
        _EMPTY_RECOVERY_NUDGE + " Please include details.",
        "(empty)",
        "Continue the real conversation.",
        "Still part of the answer.",
        _EMPTY_RECOVERY_NUDGE,
    ]
    for history in (model_history, display_history):
        assert [message["content"] for message in history] == expected
