"""Recovery for legacy compression parents with no continuation child."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import recover_rotated_compression_session
from hermes_state import CompressionSessionClosedError, SessionDB


def test_recover_rotated_compression_session_reopens_legacy_orphan(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("orphan", source="cli")
        db.append_message("orphan", "user", "before compression")
        db.end_session("orphan", "compression")
        agent = SimpleNamespace(_session_db=db, session_id="orphan")

        assert recover_rotated_compression_session(agent) is None
        db.append_message("orphan", "user", "after recovery")
    finally:
        db.close()


def test_recover_rotated_compression_session_keeps_parent_closed_with_child(
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="cli")
        db.append_message("parent", "user", "before compression")
        db.end_session("parent", "compression")
        db.create_session("child", source="cli", parent_session_id="parent")
        agent = SimpleNamespace(_session_db=db, session_id="parent")

        assert recover_rotated_compression_session(agent) is None
        try:
            db.append_message("parent", "user", "must stay closed")
        except CompressionSessionClosedError:
            pass
        else:
            raise AssertionError("compression parent with child was reopened")
    finally:
        db.close()


def test_multi_generation_recovery_preserves_immediate_parent_compression_state(
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="telegram")
        db.append_message("root", "user", "root transcript")
        db.end_session("root", "compression")
        db.create_session("child", source="telegram", parent_session_id="root")
        db.append_message("child", "user", "child handoff")
        db.end_session("child", "compression")
        db.create_session("tip", source="telegram", parent_session_id="child")
        db.append_message("tip", "user", "tip handoff")

        db.set_compression_fallback_streak("root", 1)
        db.set_compression_ineffective_count("root", 1)
        db.set_compression_fallback_streak("child", 4)
        db.set_compression_ineffective_count("child", 2)
        db.set_compression_fallback_streak("tip", 4)
        db.set_compression_ineffective_count("tip", 2)

        with patch(
            "agent.context_compressor.get_model_context_length",
            return_value=100_000,
        ):
            compressor = ContextCompressor(
                model="test/model",
                threshold_percent=0.85,
                protect_first_n=2,
                protect_last_n=2,
                quiet_mode=True,
            )
        compressor.bind_session_state(db, "root")
        agent = SimpleNamespace(
            _session_db=db,
            session_id="root",
            context_compressor=compressor,
            _memory_manager=None,
            platform="telegram",
            _gateway_session_key="agent:main:telegram:dm:test",
        )

        recovered = recover_rotated_compression_session(agent)

        assert recovered is not None
        assert agent.session_id == "tip"
        assert compressor._fallback_compression_streak == 4
        assert compressor._ineffective_compression_count == 2
        assert db.get_compression_ineffective_count("tip") == 2
    finally:
        db.close()
