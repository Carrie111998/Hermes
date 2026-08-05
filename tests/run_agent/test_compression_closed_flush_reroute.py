"""Regression coverage for agent flushes that lose a compression race."""

from __future__ import annotations

import os
from unittest.mock import patch

from hermes_state import SessionDB


def _make_agent(db: SessionDB, session_id: str):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._ensure_db_session()
    return agent


def test_flush_adopts_unique_live_child_when_parent_was_compressed(tmp_path) -> None:
    """A stale turn must persist to the winner's child instead of stopping."""
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_id = "parent"
    child_id = "child"
    agent = _make_agent(db, parent_id)

    old_message = {"role": "user", "content": "already persisted"}
    db.append_message(
        parent_id,
        role=old_message["role"],
        content=old_message["content"],
    )
    db.end_session(parent_id, "compression")
    db.create_session(child_id, source="test", parent_session_id=parent_id)
    db.replace_messages(
        child_id,
        [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}],
    )

    current_user = {"role": "user", "content": "arrived during compression"}
    current_reply = {"role": "assistant", "content": "completed safely"}
    messages = [old_message, current_user, current_reply]

    with patch.object(
        SessionDB,
        "find_live_compression_child",
        side_effect=AssertionError("reroute must resolve and append in one transaction"),
    ):
        assert agent._flush_messages_to_session_db(
            messages,
            conversation_history=[old_message],
        ) is True
    assert getattr(agent, "session_id", None) == child_id
    assert agent._flushed_db_message_session_id == child_id
    assert [row["content"] for row in db.get_messages(child_id)] == [
        "[CONTEXT COMPACTION] summary",
        "arrived during compression",
        "completed safely",
    ]
    assert all(message.get("_db_persisted") is True for message in messages)
    assert agent._flush_messages_to_session_db(
        messages,
        conversation_history=[old_message],
    ) is True
    assert [row["content"] for row in db.get_messages(child_id)] == [
        "[CONTEXT COMPACTION] summary",
        "arrived during compression",
        "completed safely",
    ]
    db.close()


def test_flush_classifies_ambiguous_compression_continuation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_id = "parent"
    db.create_session(parent_id, "slack")
    db.end_session(parent_id, "compression")

    for child_id in ("child-a", "child-b"):
        db.create_session(
            child_id,
            "slack",
            parent_session_id=parent_id,
        )
        db.replace_messages(
            child_id,
            [{"role": "user", "content": f"Summary for {child_id}"}],
        )

    agent = _make_agent(db, parent_id)
    current_user = {"role": "user", "content": "Continue the task"}
    assistant = {"role": "assistant", "content": "Current answer"}
    messages = [current_user, assistant]

    persisted = agent._flush_messages_to_session_db(
        messages,
        conversation_history=[],
    )

    assert persisted is False
    assert agent.session_id == parent_id
    assert getattr(agent, "_persistence_failure_reason", None) == (
        "compression_session_closed"
    )
    assert all(message.get("_db_persisted") is not True for message in messages)
    for child_id in ("child-a", "child-b"):
        assert [row["content"] for row in db.get_messages(child_id)] == [
            f"Summary for {child_id}"
        ]

    explanation = agent._format_turn_completion_explanation(
        "compression_session_closed"
    )
    assert "compression continuation" in explanation.lower()
    assert "disk" not in explanation.lower()
    db.close()
