"""Regression for final flushes that race a compression rotation."""

from types import SimpleNamespace

from hermes_state import SessionDB
from run_agent import AIAgent


def _bare_agent(db: SessionDB, session_id: str) -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = session_id
    agent._db_flush_scan_prefix = None
    agent._active_compression_lock_holder = None
    agent.context_compressor = SimpleNamespace()
    agent._memory_manager = None
    agent.platform = "telegram"
    agent._gateway_session_key = "agent:main:telegram:dm:test"
    return agent


def test_final_flush_adopts_unique_live_compression_child(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="telegram")
        db.append_message("parent", "user", "before compression")
        db.publish_compression_child(
            parent_session_id="parent",
            child_session_id="child",
            source="telegram",
            messages=[{"role": "user", "content": "compressed handoff"}],
            require_compression_lease=False,
        )
        agent = _bare_agent(db, "parent")
        final = {"role": "assistant", "content": "finished after compression"}

        result = agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        )

        assert result is True
        assert agent.session_id == "child"
        assert agent._flushed_db_message_session_id == "child"
        assert final.get("_db_persisted") is True
        assert [row["content"] for row in db.get_messages("parent")] == [
            "before compression"
        ]
        assert [row["content"] for row in db.get_messages("child")] == [
            "compressed handoff",
            "finished after compression",
        ]
    finally:
        db.close()
