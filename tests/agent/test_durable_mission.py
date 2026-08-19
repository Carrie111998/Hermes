"""P1 durable mission state: SessionDB authority and pre-LLM restore."""

import inspect
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.durable_mission import (
    CHECKPOINT_SCHEMA_VERSION,
    MissionCheckpoint,
    MissionCheckpointCompatibilityError,
    MissionCheckpointIntegrityError,
    MissionCheckpointRequiredError,
    MissionStateError,
    render_mission_projection,
    restore_mission_for_turn,
    validate_checkpoint,
)
from hermes_state import SessionDB


def _checkpoint(**overrides):
    values = {
        "mission_id": "mission-1",
        "checkpoint_id": "checkpoint-1",
        "parent_checkpoint_id": None,
        "state_version": CHECKPOINT_SCHEMA_VERSION,
        "objective": "ship durable state",
        "phase": "implementation",
        "completed_steps": ["baseline"],
        "pending_steps": ["restore"],
        "blocker": None,
        "blocking_unknown": None,
        "next_action": "implement checkpoint restore",
        "forbidden_retries": ["do not replay provider call"],
        "terminal_state": None,
        "canonical_repo": "/repo",
        "repo_observed_head": "abc123",
        "codegraph_project": "/repo",
        "codegraph_fingerprint": "cg-1",
        "approval_reference": {"approval_id": "a1", "observed_status": "UNKNOWN"},
        "safety_reference": {"observed_status": "UNKNOWN"},
        "financial_reference": {"observed_status": "UNKNOWN"},
        "convergence_reference": {"observed_status": "UNKNOWN"},
    }
    values.update(overrides)
    return MissionCheckpoint(**values)


def _db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _mission(db, mission_id="mission-1", session_id="session-a"):
    db.create_session(session_id, "test")
    db.create_mission(mission_id, root_session_id=session_id)


def test_active_checkpoint_requires_next_action():
    with pytest.raises(MissionCheckpointIntegrityError):
        validate_checkpoint(_checkpoint(next_action=None))


def test_blocked_checkpoint_requires_blocker():
    with pytest.raises(MissionCheckpointIntegrityError):
        validate_checkpoint(_checkpoint(status="BLOCKED", blocker=None))


def test_terminal_checkpoint_forbids_next_action():
    with pytest.raises(MissionCheckpointIntegrityError):
        validate_checkpoint(
            _checkpoint(status="TERMINAL", terminal_state="complete", next_action="retry")
        )


def test_blocking_unknown_requires_blocker():
    with pytest.raises(MissionCheckpointIntegrityError):
        validate_checkpoint(_checkpoint(blocking_unknown="approval read unavailable", blocker=None))


def test_invalid_state_rejected():
    with pytest.raises(MissionCheckpointIntegrityError):
        validate_checkpoint(_checkpoint(status="ACTIVE", terminal_state="complete"))


def test_unsupported_checkpoint_version_fails_closed():
    with pytest.raises(MissionCheckpointCompatibilityError):
        validate_checkpoint(_checkpoint(state_version=CHECKPOINT_SCHEMA_VERSION + 1))


def test_projection_is_deterministic_and_bounded():
    checkpoint = _checkpoint()
    first = render_mission_projection(checkpoint)
    second = render_mission_projection(replace(checkpoint))
    assert first == second
    assert "MISSION_ID: mission-1" in first
    assert "NEXT_ACTION: implement checkpoint restore" in first
    assert "Conversation memory is non-authoritative." in first


def test_projection_rejects_unbounded_state():
    with pytest.raises(MissionCheckpointIntegrityError):
        render_mission_projection(
            _checkpoint(completed_steps=["x" * 4096] * 100)
        )


def test_checkpoint_fields_win_over_conversation_summary():
    projection = render_mission_projection(_checkpoint(next_action="Y"))
    assert "NEXT_ACTION: Y" in projection
    assert "NEXT_ACTION: X" not in projection


def test_mission_id_is_distinct_from_session_id(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    assert db.get_mission("mission-1")["mission_id"] != "session-a"


def test_mission_id_created_and_checkpoint_persisted_atomically(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    mission = db.get_mission("mission-1")
    assert mission["current_checkpoint_id"] == "checkpoint-1"
    assert db.load_mission_checkpoint("mission-1").checkpoint_id == "checkpoint-1"


def test_checkpoint_parent_lineage_is_required(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db.write_mission_checkpoint(
        _checkpoint(
            checkpoint_id="checkpoint-2",
            parent_checkpoint_id="checkpoint-1",
            next_action="continue",
        )
    )
    assert db.load_mission_checkpoint("mission-1").parent_checkpoint_id == "checkpoint-1"


def test_wrong_checkpoint_parent_is_rejected(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    with pytest.raises(MissionCheckpointIntegrityError):
        db.write_mission_checkpoint(
            _checkpoint(checkpoint_id="checkpoint-2", parent_checkpoint_id="wrong")
        )


def test_checkpoint_survives_sessiondb_reopen(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db.close()
    reopened = _db(tmp_path)
    assert reopened.load_mission_checkpoint("mission-1").next_action == "implement checkpoint restore"


def test_migration_is_idempotent(tmp_path):
    first = _db(tmp_path)
    first.close()
    second = _db(tmp_path)
    second.close()
    third = _db(tmp_path)
    assert third._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('missions', 'mission_checkpoints')"
    ).fetchone()[0] == 2


def test_mission_session_binding_survives_rotation(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db.create_session("session-b", "test", parent_session_id="session-a")
    db.bind_mission_session("mission-1", "session-a", "session-b")
    assert db.get_mission_for_session("session-b")["mission_id"] == "mission-1"
    assert db.get_mission_for_session("session-a")["mission_id"] == "mission-1"


def test_two_rotations_keep_one_durable_mission_id(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db.rotate_mission_session("mission-1", "session-a", "session-b", "test")
    db.rotate_mission_session("mission-1", "session-b", "session-c", "test")
    assert db.get_mission_for_session("session-a")["mission_id"] == "mission-1"
    assert db.get_mission_for_session("session-b")["mission_id"] == "mission-1"
    assert db.get_mission_for_session("session-c")["mission_id"] == "mission-1"
    assert db.get_mission("mission-1")["current_session_id"] == "session-c"


def test_restore_missing_checkpoint_fails_closed(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    agent = SimpleNamespace(mission_id="mission-1", _session_db=db)
    with pytest.raises(MissionCheckpointRequiredError):
        restore_mission_for_turn(agent)


def test_restore_corrupt_checkpoint_fails_closed(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db._conn.execute("UPDATE mission_checkpoints SET next_action = NULL")
    db._conn.commit()
    with pytest.raises(MissionCheckpointIntegrityError):
        restore_mission_for_turn(SimpleNamespace(mission_id="mission-1", _session_db=db))


def test_restore_incompatible_checkpoint_fails_closed(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    db._conn.execute("UPDATE mission_checkpoints SET state_version = ?", (CHECKPOINT_SCHEMA_VERSION + 1,))
    db._conn.commit()
    with pytest.raises(MissionCheckpointCompatibilityError):
        restore_mission_for_turn(SimpleNamespace(mission_id="mission-1", _session_db=db))


def test_restore_sets_bounded_projection(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    agent = SimpleNamespace(mission_id="mission-1", _session_db=db)
    projection = restore_mission_for_turn(agent)
    assert projection == agent._durable_mission_projection
    assert agent._durable_mission_checkpoint.next_action == "implement checkpoint restore"


def test_non_durable_agent_skips_restore(tmp_path):
    agent = SimpleNamespace(mission_id=None, _session_db=_db(tmp_path))
    assert restore_mission_for_turn(agent) == ""


def test_restart_resolves_mission_from_persisted_session_binding(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    agent = SimpleNamespace(mission_id=None, session_id="session-a", _session_db=db)
    restore_mission_for_turn(agent)
    assert agent.mission_id == "mission-1"


def test_checkpoint_write_failure_is_not_silent(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.close()
    with pytest.raises(Exception):
        db.write_mission_checkpoint(_checkpoint())


def test_external_references_do_not_grant_authority():
    checkpoint = _checkpoint(
        approval_reference={"approval_id": "a1", "observed_status": "APPROVED"},
        safety_reference={"observed_status": "BLOCKED"},
        financial_reference={"observed_status": "UNKNOWN"},
    )
    projection = render_mission_projection(checkpoint)
    assert "approval_granted_by_mission_engine" not in projection
    assert "APPROVED" not in projection


def test_codegraph_binding_is_reference_only():
    projection = render_mission_projection(_checkpoint(codegraph_project="/canonical/repo"))
    assert "CODEGRAPH_PROJECT: /canonical/repo" in projection
    assert "override" not in projection.lower()


def test_codegraph_binding_mismatch_blocks_restore(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint(codegraph_project="/canonical/repo"))
    agent = SimpleNamespace(
        mission_id="mission-1", _session_db=db, _codegraph_project="/wrong/repo"
    )
    with pytest.raises(MissionCheckpointIntegrityError):
        restore_mission_for_turn(agent)


def test_mission_state_error_is_explicit():
    assert issubclass(MissionCheckpointIntegrityError, MissionStateError)


def test_turn_context_restore_is_before_message_assembly():
    from agent import turn_context

    source = inspect.getsource(turn_context.build_turn_context)
    assert source.index("agent._ensure_db_session()") < source.index("restore_mission_for_turn")
    assert source.index("restore_mission_for_turn") < source.index("messages = list")


def test_conversation_loop_provider_is_unreachable_when_restore_fails(monkeypatch):
    from agent import conversation_loop

    def fail_before_provider(*args, **kwargs):
        raise MissionCheckpointRequiredError("checkpoint missing")

    monkeypatch.setattr(conversation_loop, "build_turn_context", fail_before_provider)
    fake_agent = SimpleNamespace(_perform_api_call=lambda *_a, **_k: pytest.fail("provider reached"))
    with pytest.raises(MissionCheckpointRequiredError):
        conversation_loop.run_conversation(fake_agent, "hello")


@pytest.mark.parametrize(
    ("surface_file", "construction_marker", "turn_marker"),
    [
        ("cli.py", "def AIAgent", "run_conversation("),
        ("tui_gateway/server.py", "return AIAgent(", "agent.run_conversation("),
        ("gateway/platforms/api_server.py", "agent = AIAgent(", "agent.run_conversation("),
    ],
)
def test_supported_surfaces_reach_canonical_turn_path(
    surface_file, construction_marker, turn_marker
):
    source = (Path(__file__).parents[2] / surface_file).read_text()
    assert construction_marker in source
    assert turn_marker in source
    assert "agent.conversation_loop" in (
        Path(__file__).parents[2] / "run_agent.py"
    ).read_text()


def test_post_checkpoint_update_projection_uses_current_checkpoint(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(_checkpoint())
    agent = SimpleNamespace(mission_id="mission-1", _session_db=db)
    restore_mission_for_turn(agent)
    db.write_mission_checkpoint(
        _checkpoint(
            checkpoint_id="checkpoint-2",
            parent_checkpoint_id="checkpoint-1",
            next_action="new current action",
        )
    )
    restore_mission_for_turn(agent)
    assert "NEXT_ACTION: new current action" in agent._durable_mission_projection


def test_compression_wiring_uses_durable_rotation_adapter():
    from agent import conversation_compression

    source = inspect.getsource(conversation_compression.compress_context)
    assert "rotate_mission_session" in source
    assert "getattr(agent, \"mission_id\", None)" in source
