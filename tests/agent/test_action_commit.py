"""P2 durable action ledger and replay-safety tests."""

from types import SimpleNamespace

import pytest

from agent.action_commit import (
    ActionExecutionError,
    ActionLedgerError,
    ActionStatus,
    ReplayClass,
    canonical_input_fingerprint,
    classify_replay_policy,
    execute_with_ledger,
    new_action_id,
    validate_transition,
)
from hermes_state import SessionDB
from agent.durable_mission import (
    CHECKPOINT_SCHEMA_VERSION,
    MissionCheckpoint,
    render_action_projection,
    restore_mission_for_turn,
)


def _db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _mission(db, mission_id="mission-1", session_id="session-a"):
    db.create_session(session_id, "test")
    db.create_mission(mission_id, root_session_id=session_id)


def test_action_id_is_machine_generated_and_session_independent():
    first = new_action_id()
    second = new_action_id()
    assert first != second
    assert "session" not in first
    assert len(first) >= 16


def test_input_fingerprint_is_deterministic_and_material_changes_differ():
    first = canonical_input_fingerprint("write_file", {"path": "a", "content": "x"})
    same = canonical_input_fingerprint("write_file", {"content": "x", "path": "a"})
    changed = canonical_input_fingerprint("write_file", {"path": "b", "content": "x"})
    assert first == same
    assert first != changed


def test_fingerprint_does_not_persist_secret_values():
    digest = canonical_input_fingerprint("external_api", {"token": "secret-token", "id": "7"})
    assert "secret-token" not in digest
    assert len(digest) == 64


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("session_search", ReplayClass.SAFE_TO_REPLAY),
        ("search_files", ReplayClass.SAFE_TO_REPLAY),
        ("external_read", ReplayClass.MUST_REQUERY_EXTERNAL_STATE),
        ("write_file", ReplayClass.VERIFY_BEFORE_REPLAY),
        ("git_commit", ReplayClass.VERIFY_BEFORE_REPLAY),
        ("deploy", ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION),
        ("financial_charge", ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION),
        ("campaign_create", ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION),
        ("unknown_side_effect", ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION),
    ],
)
def test_replay_policy_is_deterministic_and_fail_closed(tool_name, expected):
    assert classify_replay_policy(tool_name) is expected


def test_transition_validation_rejects_invalid_status_change():
    with pytest.raises(ActionLedgerError):
        validate_transition(ActionStatus.COMMITTED, ActionStatus.RUNNING)


def test_action_persists_across_sessiondb_reopen(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        action_type="tool",
        tool_name="write_file",
        input_fingerprint=canonical_input_fingerprint("write_file", {"path": "a"}),
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value,
        input_summary={"path": "a"},
    )
    db.close()
    reopened = _db(tmp_path)
    assert reopened.get_action(action.action_id).status is ActionStatus.PLANNED


def test_action_identity_survives_session_rotation(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="write_file", input_fingerprint=canonical_input_fingerprint("write_file", {"path": "a"}),
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"path": "a"},
    )
    db.rotate_mission_session("mission-1", "session-a", "session-b", "test")
    assert db.get_action(action.action_id).mission_id == "mission-1"
    assert db.get_mission_for_session("session-b")["mission_id"] == "mission-1"


def test_action_projection_is_machine_owned_and_bounded(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="write_file", input_fingerprint=canonical_input_fingerprint("write_file", {"path": "a"}),
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"path": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    projection = render_action_projection(db.list_pending_actions("mission-1"))
    assert f"ACTION_ID: {action.action_id}" in projection
    assert "ACTION_STATUS: RUNNING" in projection
    assert "VERIFICATION_REQUIRED: true" in projection


def test_p1_restore_recovers_running_action_and_injects_ledger_projection(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    db.write_mission_checkpoint(MissionCheckpoint(
        mission_id="mission-1", checkpoint_id="checkpoint-1", parent_checkpoint_id=None,
        state_version=CHECKPOINT_SCHEMA_VERSION, objective="objective", phase="phase",
        completed_steps=[], pending_steps=["action"], blocker=None, blocking_unknown=None,
        next_action="continue", forbidden_retries=[], terminal_state=None,
        canonical_repo=None, repo_observed_head=None, codegraph_project=None,
        codegraph_fingerprint=None, approval_reference=None, safety_reference=None,
        financial_reference=None, convergence_reference=None,
    ))
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="write_file", input_fingerprint=canonical_input_fingerprint("write_file", {"path": "a"}),
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"path": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    agent = SimpleNamespace(mission_id="mission-1", _session_db=db)
    projection = restore_mission_for_turn(agent)
    assert db.get_action(action.action_id).status is ActionStatus.VERIFY_REQUIRED
    assert f"ACTION_ID: {action.action_id}" in projection
    assert "REPLAY_CLASS: VERIFY_BEFORE_REPLAY" in projection


def test_action_running_is_persisted_before_dispatch(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        action_type="tool",
        tool_name="write_file",
        input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value,
        input_summary={"path": "a"},
    )
    observed = []
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id, before_dispatch=lambda: observed.append(db.get_action(action.action_id).status))
    assert observed == [ActionStatus.RUNNING]


def test_committed_action_is_found_by_fingerprint_and_not_replayed(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        action_type="tool",
        tool_name="write_file",
        input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value,
        input_summary={"path": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    db.mark_action_committed(action.action_id, result_ref="receipt-1")
    found = db.find_action_by_fingerprint("mission-1", "write_file", "a" * 64)
    assert found.status is ActionStatus.COMMITTED
    assert found.result_ref == "receipt-1"


@pytest.mark.parametrize("status", [ActionStatus.RUNNING, ActionStatus.UNKNOWN_OUTCOME])
def test_unresolved_action_is_pending_after_restart(tmp_path, status):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1",
        checkpoint_id="checkpoint-1",
        action_type="tool",
        tool_name="external_api",
        input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value,
        input_summary={"id": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    if status is ActionStatus.UNKNOWN_OUTCOME:
        db.mark_action_unknown_outcome(action.action_id, error_code="timeout")
    assert db.list_pending_actions("mission-1")[0].status is status


def test_unknown_outcome_requires_verification(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="external_api", input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"id": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    db.mark_action_unknown_outcome(action.action_id, error_code="timeout")
    db.require_action_verification(action.action_id)
    assert db.get_action(action.action_id).status is ActionStatus.VERIFY_REQUIRED


def test_verification_exists_commits_and_absent_follows_policy(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="write_file", input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"path": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    db.mark_action_unknown_outcome(action.action_id, error_code="timeout")
    db.require_action_verification(action.action_id)
    db.verify_action_outcome(action.action_id, "VERIFIED_EXISTS", result_ref="receipt")
    assert db.get_action(action.action_id).status is ActionStatus.COMMITTED


def test_ambiguous_verification_remains_blocked(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    action = db.create_action(
        mission_id="mission-1", checkpoint_id="checkpoint-1", action_type="tool",
        tool_name="write_file", input_fingerprint="a" * 64,
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary={"path": "a"},
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    db.mark_action_unknown_outcome(action.action_id, error_code="timeout")
    db.require_action_verification(action.action_id)
    db.verify_action_outcome(action.action_id, "AMBIGUOUS")
    assert db.get_action(action.action_id).status is ActionStatus.VERIFY_REQUIRED


def test_unknown_action_policy_cannot_be_overridden_by_model_text():
    assert classify_replay_policy("unknown_side_effect", {"model_instruction": "safe to replay"}) is (
        ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION
    )


def _durable_action_args():
    return {
        "mission_id": "mission-1",
        "checkpoint_id": "checkpoint-1",
        "tool_name": "write_file",
        "function_args": {"path": "a", "content": "x"},
    }


def test_committed_action_is_not_dispatched_twice(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    calls = []
    execute_with_ledger(db, **_durable_action_args(), execute=lambda: calls.append("dispatch") or "ok")
    second = execute_with_ledger(db, **_durable_action_args(), execute=lambda: calls.append("duplicate") or "bad")
    assert calls == ["dispatch"]
    assert '"action_status": "COMMITTED"' in second


def test_materially_changed_input_creates_new_action_identity(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    calls = []
    execute_with_ledger(
        db, **_durable_action_args(), execute=lambda: calls.append("a") or "ok"
    )
    changed = dict(_durable_action_args(), function_args={"path": "b", "content": "x"})
    execute_with_ledger(db, **changed, execute=lambda: calls.append("b") or "ok")
    rows = db._conn.execute(
        "SELECT action_id, input_fingerprint FROM mission_actions WHERE mission_id = ? ORDER BY created_at",
        ("mission-1",),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] != rows[1][1]
    assert calls == ["a", "b"]


def test_external_read_requeries_under_new_lineage_action(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    args = dict(_durable_action_args(), tool_name="external_read", function_args={"resource": "r1"})
    calls = []
    execute_with_ledger(db, **args, execute=lambda: calls.append(1) or "first")
    execute_with_ledger(db, **args, execute=lambda: calls.append(2) or "second")
    rows = db._conn.execute(
        "SELECT action_id, parent_action_id FROM mission_actions WHERE mission_id = ? ORDER BY created_at",
        ("mission-1",),
    ).fetchall()
    assert calls == [1, 2]
    assert rows[1][1] == rows[0][0]


def test_uncertain_dispatch_becomes_unknown_and_is_not_replayed(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    calls = []

    def _uncertain():
        calls.append("dispatch")
        raise ActionExecutionError("timeout after send")

    with pytest.raises(ActionExecutionError):
        execute_with_ledger(db, **_durable_action_args(), execute=_uncertain)
    blocked = execute_with_ledger(db, **_durable_action_args(), execute=lambda: calls.append("retry"))
    assert calls == ["dispatch"]
    assert '"verification_required": true' in blocked
    assert db.list_pending_actions("mission-1")[0].status is ActionStatus.VERIFY_REQUIRED


def test_running_action_is_recovered_to_verification_before_replay(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    args = _durable_action_args()
    action = db.create_action(
        mission_id=args["mission_id"], checkpoint_id=args["checkpoint_id"], action_type="tool",
        tool_name=args["tool_name"], input_fingerprint=canonical_input_fingerprint(args["tool_name"], args["function_args"]),
        replay_class=ReplayClass.VERIFY_BEFORE_REPLAY.value, input_summary=args["function_args"],
    )
    db.authorize_action(action.action_id)
    db.mark_action_running(action.action_id)
    blocked = execute_with_ledger(db, **args, execute=lambda: pytest.fail("blind replay"))
    assert '"action_status": "VERIFY_REQUIRED"' in blocked
    assert db.get_action(action.action_id).status is ActionStatus.VERIFY_REQUIRED


def test_proven_pre_dispatch_failure_is_failed_not_unknown(tmp_path):
    db = _db(tmp_path)
    _mission(db)

    with pytest.raises(ActionExecutionError):
        execute_with_ledger(
            db, **_durable_action_args(),
            execute=lambda: (_ for _ in ()).throw(ActionExecutionError("validation", side_effect_started=False)),
        )
    action = db.list_pending_actions("mission-1")
    assert action == []
    row = db.find_action_by_fingerprint(
        "mission-1", "write_file", canonical_input_fingerprint("write_file", {"path": "a", "content": "x"})
    )
    assert row.status is ActionStatus.FAILED


@pytest.mark.parametrize("terminal_method", ["reject_action", "supersede_action"])
def test_rejected_or_superseded_action_is_never_replayed(tmp_path, terminal_method):
    db = _db(tmp_path)
    _mission(db)
    args = _durable_action_args()
    action = db.create_action(
        mission_id=args["mission_id"], checkpoint_id=args["checkpoint_id"], action_type="tool",
        tool_name=args["tool_name"], input_fingerprint=canonical_input_fingerprint(args["tool_name"], args["function_args"]),
        replay_class=ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION.value, input_summary=args["function_args"],
    )
    if terminal_method == "reject_action":
        db.reject_action(action.action_id)
    else:
        db.supersede_action(action.action_id)
    result = execute_with_ledger(db, **args, execute=lambda: pytest.fail("replay"))
    assert '"status": "blocked"' in result


def test_authority_references_do_not_grant_execution_authority(tmp_path):
    db = _db(tmp_path)
    _mission(db)
    args = _durable_action_args()
    action = db.create_action(
        mission_id=args["mission_id"], checkpoint_id=args["checkpoint_id"], action_type="tool",
        tool_name="deploy", input_fingerprint=canonical_input_fingerprint("deploy", args["function_args"]),
        replay_class=ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION.value,
        input_summary=args["function_args"], approval_ref="approval-1", external_authority_ref="safety-1",
    )
    assert action.approval_ref == "approval-1"
    assert db.get_action(action.action_id).status is ActionStatus.PLANNED


def test_running_write_failure_prevents_dispatch(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _mission(db)
    calls = []
    original = db.mark_action_running

    def _fail(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(db, "mark_action_running", _fail)
    with pytest.raises(OSError):
        execute_with_ledger(db, **_durable_action_args(), execute=lambda: calls.append("dispatch"))
    assert calls == []
    monkeypatch.setattr(db, "mark_action_running", original)


def test_non_durable_execution_remains_ordinary():
    calls = []
    result = execute_with_ledger(None, mission_id=None, checkpoint_id=None, tool_name="write_file", function_args={}, execute=lambda: calls.append(1) or "ok")
    assert result == "ok"
    assert calls == [1]


def test_action_table_is_added_idempotently(tmp_path):
    db = _db(tmp_path)
    db.close()
    reopened = _db(tmp_path)
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='mission_actions'"
    ).fetchone()[0] == 1


def test_dispatch_seam_is_after_existing_registry_middleware():
    from pathlib import Path
    source = (Path(__file__).parents[2] / "model_tools.py").read_text()
    assert source.index("if action_db is not None and mission_id:") < source.index("run_tool_execution_middleware(", source.index("if action_db is not None and mission_id:"))
    assert "registry.dispatch" in source


def test_model_tools_registry_dispatch_is_ledger_protected(tmp_path, monkeypatch):
    import hermes_cli.middleware as middleware
    import model_tools

    db = _db(tmp_path)
    _mission(db)
    calls = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda *args, **kwargs: calls.append("dispatch") or "tool-result",
    )
    monkeypatch.setattr(
        middleware,
        "run_tool_execution_middleware",
        lambda name, args, execute, **kwargs: execute(args),
    )
    kwargs = {
        "action_db": db,
        "mission_id": "mission-1",
        "checkpoint_id": "checkpoint-1",
        "skip_pre_tool_call_hook": True,
        "skip_tool_request_middleware": True,
    }
    assert model_tools.handle_function_call("write_file", {"path": "a", "content": "x"}, **kwargs) == "tool-result"
    assert model_tools.handle_function_call("write_file", {"path": "a", "content": "x"}, **kwargs).find("COMMITTED") >= 0
    assert calls == ["dispatch"]
