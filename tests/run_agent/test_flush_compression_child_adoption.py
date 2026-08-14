"""Regression for final flushes that race a compression rotation."""

from types import SimpleNamespace

from agent.conversation_compression import recover_rotated_compression_session
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


def test_final_flush_adopts_unique_multi_generation_compression_tip(tmp_path) -> None:
    """A stale long-running turn may lag behind several durable rotations."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="telegram")
        db.append_message("parent", "user", "before first compression")
        db.publish_compression_child(
            parent_session_id="parent",
            child_session_id="child",
            source="telegram",
            messages=[{"role": "user", "content": "first compressed handoff"}],
            require_compression_lease=False,
        )
        db.publish_compression_child(
            parent_session_id="child",
            child_session_id="grandchild",
            source="telegram",
            messages=[{"role": "user", "content": "second compressed handoff"}],
            require_compression_lease=False,
        )
        agent = _bare_agent(db, "parent")
        final = {"role": "assistant", "content": "finished after two rotations"}

        result = agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        )

        assert result is True
        assert agent.session_id == "grandchild"
        assert agent._flushed_db_message_session_id == "grandchild"
        assert final.get("_db_persisted") is True
        assert [row["content"] for row in db.get_messages("grandchild")] == [
            "second compressed handoff",
            "finished after two rotations",
        ]
    finally:
        db.close()


def test_final_flush_revalidates_tip_when_lineage_changes_before_retry(
    tmp_path, monkeypatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="telegram")
        db.append_message("parent", "user", "before first compression")
        db.publish_compression_child(
            parent_session_id="parent",
            child_session_id="child",
            source="telegram",
            messages=[{"role": "user", "content": "first handoff"}],
            require_compression_lease=False,
        )
        db.publish_compression_child(
            parent_session_id="child",
            child_session_id="grandchild",
            source="telegram",
            messages=[{"role": "user", "content": "second handoff"}],
            require_compression_lease=False,
        )
        original_append = db.append_messages_batch
        injected = False

        def racing_append(*args, **kwargs):
            nonlocal injected
            session_id = kwargs.get("session_id") or args[0]
            if session_id == "grandchild" and not injected:
                injected = True
                db.create_session(
                    "ambiguous-sibling",
                    source="telegram",
                    parent_session_id="parent",
                )
            return original_append(*args, **kwargs)

        monkeypatch.setattr(db, "append_messages_batch", racing_append)
        agent = _bare_agent(db, "parent")
        final = {"role": "assistant", "content": "must fail closed"}

        result = agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        )

        assert result is False
        assert agent.session_id == "parent"
        assert final.get("_db_persisted") is not True

        retry_result = agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        )

        assert retry_result is False
        assert agent.session_id == "parent"
        assert final.get("_db_persisted") is not True
        assert [row["content"] for row in db.get_messages("grandchild")] == [
            "second handoff"
        ]
    finally:
        db.close()


def test_turn_start_recovery_keeps_root_guard_until_first_durable_flush(
    tmp_path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="telegram")
        db.append_message("root", "user", "before compression")
        db.publish_compression_child(
            parent_session_id="root",
            child_session_id="tip",
            source="telegram",
            messages=[{"role": "user", "content": "compressed handoff"}],
            require_compression_lease=False,
        )
        agent = _bare_agent(db, "root")

        recovered = recover_rotated_compression_session(agent)
        assert recovered is not None
        assert agent.session_id == "tip"

        # The lineage becomes ambiguous after turn-start recovery but before
        # this agent's first durable write on the adopted tip.
        db.create_session(
            "late-sibling",
            source="telegram",
            parent_session_id="root",
        )
        final = {"role": "assistant", "content": "must fail closed"}

        result = agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        )

        assert result is False
        assert final.get("_db_persisted") is not True
        assert [row["content"] for row in db.get_messages("tip")] == [
            "compressed handoff"
        ]
        assert getattr(agent, "_pending_compression_lineage_guards", None) == {
            "tip": "root"
        }
    finally:
        db.close()


def test_turn_start_recovery_clears_guard_after_first_durable_flush(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="telegram")
        db.publish_compression_child(
            parent_session_id="root",
            child_session_id="tip",
            source="telegram",
            messages=[{"role": "user", "content": "compressed handoff"}],
            require_compression_lease=False,
        )
        agent = _bare_agent(db, "root")

        recovered = recover_rotated_compression_session(agent)
        assert recovered is not None
        assert getattr(agent, "_pending_compression_lineage_guards", None) == {
            "tip": "root"
        }
        # Recovery must bind durable rows with intrinsic markers, not raw
        # id() values that can alias a newly allocated final message after the
        # caller releases the recovered list.
        assert agent._flushed_db_message_ids == set()
        assert all(message.get("_db_persisted") is True for message in recovered)
        del recovered
        final = {"role": "assistant", "content": "guarded durable write"}

        assert agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        ) is True

        assert getattr(agent, "_pending_compression_lineage_guards", None) == {}
        assert final.get("_db_persisted") is True
        assert [row["content"] for row in db.get_messages("tip")] == [
            "compressed handoff",
            "guarded durable write",
        ]
    finally:
        db.close()


def test_first_flush_follows_new_tip_with_original_root_guard(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="telegram")
        db.publish_compression_child(
            parent_session_id="root",
            child_session_id="tip",
            source="telegram",
            messages=[{"role": "user", "content": "first handoff"}],
            require_compression_lease=False,
        )
        agent = _bare_agent(db, "root")
        assert recover_rotated_compression_session(agent) is not None

        db.publish_compression_child(
            parent_session_id="tip",
            child_session_id="new-tip",
            source="telegram",
            messages=[{"role": "user", "content": "second handoff"}],
            require_compression_lease=False,
        )
        final = {"role": "assistant", "content": "write on newest tip"}

        assert agent._flush_messages_to_session_db_unlocked(
            [final], conversation_history=[]
        ) is True

        assert agent.session_id == "new-tip"
        assert getattr(agent, "_pending_compression_lineage_guards", None) == {}
        assert final.get("_db_persisted") is True
        assert [row["content"] for row in db.get_messages("new-tip")] == [
            "second handoff",
            "write on newest tip",
        ]
    finally:
        db.close()
