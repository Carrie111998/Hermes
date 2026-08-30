"""Model-callable goal control at the real registry dispatch seam."""

import asyncio
import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

from hermes_cli import goals
from model_tools import get_tool_definitions, handle_function_call


def _call(action: str, *, session_id: str, **kwargs):
    raw = handle_function_call(
        "goal_control",
        {"action": action, **kwargs},
        session_id=session_id,
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    return json.loads(raw)


def test_status_returns_a_session_scoped_goal_readback_receipt(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Run checks", max_turns=3)

    raw = handle_function_call(
        "goal_control",
        {"action": "status"},
        session_id="session-current",
        tool_call_id="call-status-1",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )
    result = json.loads(raw)

    receipt = result["goal_readback"]
    receipt_prefix = "goal_control:session-current:"
    receipt_id = receipt.pop("receipt_id")
    assert receipt_id.startswith(receipt_prefix)
    assert receipt_id[len(receipt_prefix):]
    assert receipt == {
        "kind": "goal-status-readback",
        "session_id": "session-current",
        "active": True,
        "condition": "Run checks",
        "observed_via": "goal_control",
    }


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals._DB_BOOTSTRAP_INFLIGHT.clear()
    goals._GOAL_GENERATIONS.clear()
    goals._GOAL_STATE_LOCKS.clear()
    return home


def test_set_returns_authoritative_persisted_current_session_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    result = _call(
        "set",
        session_id="session-current",
        condition="Run the focused checks",
        max_turns=7,
    )

    assert result["success"] is True
    assert result["session_id"] == "session-current"
    assert result["state"] == {
        "exists": True,
        "active": True,
        "paused": False,
        "status": "active",
        "condition": "Run the focused checks",
        "turns_used": 0,
        "max_turns": 7,
        "revision": None,
        "stop_reason": None,
        "error_reason": None,
    }
    persisted = goals.load_goal("session-current")
    assert persisted is not None
    assert persisted.goal == "Run the focused checks"
    assert persisted.max_turns == 7


def test_goal_control_is_exposed_on_the_core_model_surface(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    definitions = get_tool_definitions(
        enabled_toolsets=["hermes-cli"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )

    names = {item["function"]["name"] for item in definitions}
    assert "goal_control" in names


def test_set_uses_configured_goal_budget_when_omitted(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("goals:\n  max_turns: 11\n", encoding="utf-8")

    result = _call("set", session_id="session-current", condition="Configured budget")

    assert result["success"] is True
    assert result["state"]["max_turns"] == 11


def test_status_requires_calling_session_identity():
    result = _call("status", session_id="")

    assert result == {
        "success": False,
        "session_id": None,
        "error": {
            "code": "missing_session_identity",
            "message": "calling session identity is required",
        },
    }


def test_cross_session_argument_is_rejected_without_mutation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    raw = handle_function_call(
        "goal_control",
        {"action": "set", "condition": "wrong target", "session_id": "session-other"},
        session_id="session-current",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True,
    )

    result = json.loads(raw)
    assert result["success"] is False
    assert result["error"]["code"] == "cross_session_forbidden"
    assert goals.load_goal("session-current") is None
    assert goals.load_goal("session-other") is None


def test_pause_resume_clear_and_repeats_return_persisted_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Keep going", max_turns=4)

    for _ in range(2):
        paused = _call("pause", session_id="session-current")
        assert paused["success"] is True
        assert paused["state"]["paused"] is True
        assert paused["state"]["stop_reason"] == "model-paused"

    for _ in range(2):
        resumed = _call("resume", session_id="session-current")
        assert resumed["success"] is True
        assert resumed["state"]["active"] is True
        assert resumed["state"]["turns_used"] == 0

    for _ in range(2):
        cleared = _call("clear", session_id="session-current")
        assert cleared["success"] is True
        assert cleared["state"]["status"] == "cleared"
        assert cleared["state"]["active"] is False

    persisted = goals.load_goal("session-current")
    assert persisted is not None
    assert persisted.status == "cleared"


def test_model_pause_updates_cached_cli_lifecycle_manager(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from cli import HermesCLI

    session_id = "session-current"
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = session_id
    cli.agent = MagicMock(session_id=session_id)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = [
        {"role": "assistant", "content": "work completed this turn"}
    ]
    cli._goal_manager = goals.GoalManager(session_id=session_id, default_max_turns=4)
    cli._goal_manager.set("Keep going")

    paused = _call("pause", session_id=session_id)
    assert paused["success"] is True

    with patch("hermes_cli.goals.judge_goal") as judge:
        cli._maybe_continue_goal_after_turn()

    judge.assert_not_called()
    assert cli._pending_input.empty()
    assert cli._goal_manager.state.status == "paused"
    assert goals.load_goal(session_id).status == "paused"


def test_resume_does_not_reactivate_cleared_goal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="Terminal goal")
    _call("clear", session_id="session-current")

    resumed = _call("resume", session_id="session-current")

    assert resumed["success"] is False
    assert resumed["error"]["code"] == "invalid_transition"
    assert goals.load_goal("session-current").status == "cleared"


def test_failed_cli_refresh_does_not_execute_stale_goal(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from cli import HermesCLI

    session_id = "session-current"
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = session_id
    cli.agent = MagicMock(session_id=session_id)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = [
        {"role": "assistant", "content": "work completed this turn"}
    ]
    cli._goal_manager = goals.GoalManager(session_id=session_id, default_max_turns=4)
    cli._goal_manager.set("Keep going")

    paused = _call("pause", session_id=session_id)
    assert paused["success"] is True

    def _failed_readback(_session_id):
        raise RuntimeError("storage temporarily unavailable")

    monkeypatch.setattr(goals, "load_goal_authoritative", _failed_readback)
    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "more work", False, None, False),
    ) as judge:
        cli._maybe_continue_goal_after_turn()

    judge.assert_not_called()
    assert cli._pending_input.empty()
    assert goals.load_goal(session_id).status == "paused"


def test_update_replaces_condition_and_budget(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _call("set", session_id="session-current", condition="First", max_turns=2)

    updated = _call(
        "update",
        session_id="session-current",
        condition="Second",
        max_turns=9,
    )

    assert updated["success"] is True
    assert updated["state"]["condition"] == "Second"
    assert updated["state"]["max_turns"] == 9


def test_status_returns_stop_and_error_reasons_from_persisted_state(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    state = goals.GoalState(
        goal="Repair judge routing",
        status="paused",
        turns_used=3,
        max_turns=8,
        paused_reason="judge unavailable",
        last_reason="transport timeout",
        consecutive_transport_failures=2,
    )
    goals.save_goal("session-current", state)

    result = _call("status", session_id="session-current")

    assert result["success"] is True
    assert result["state"]["stop_reason"] == "judge unavailable"
    assert result["state"]["error_reason"] == "transport timeout"


def test_failed_persistence_never_returns_success(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    class _DroppedWriteDB:
        def get_meta(self, _key):
            return None

        def set_meta(self, _key, _value):
            return None

    monkeypatch.setattr(goals, "_get_session_db", lambda: _DroppedWriteDB())

    result = _call("set", session_id="session-current", condition="Must persist")

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_verification_failed"


def test_dropped_budget_update_is_not_mistaken_for_success(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    raw = goals.GoalState(
        goal="Same condition",
        status="active",
        max_turns=2,
    ).to_json()

    class _DroppedUpdateDB:
        def get_meta(self, _key):
            return raw

        def set_meta(self, _key, _value):
            return None

    monkeypatch.setattr(goals, "_get_session_db", lambda: _DroppedUpdateDB())

    result = _call(
        "update",
        session_id="session-current",
        condition="Same condition",
        max_turns=9,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_verification_failed"


def test_slow_initialization_fails_closed_instead_of_claiming_success(
    tmp_path, monkeypatch
):
    _home(tmp_path, monkeypatch)
    import hermes_state

    initialized = threading.Event()
    real_session_db = hermes_state.SessionDB

    class _SlowSessionDB(real_session_db):
        def __init__(self, *args, **kwargs):
            time.sleep(0.1)
            super().__init__(*args, **kwargs)
            initialized.set()

    monkeypatch.setattr(hermes_state, "SessionDB", _SlowSessionDB)
    monkeypatch.setattr(goals, "_DB_BOOTSTRAP_INIT_WAIT_S", 0.01)

    async def _on_runtime_loop():
        return _call("set", session_id="session-current", condition="Slow write")

    result = asyncio.run(_on_runtime_loop())

    assert result["success"] is False
    assert result["error"]["code"] == "persistence_unavailable"
    assert initialized.wait(2), "background SessionDB initialization did not finish"
    assert goals.load_goal("session-current") is None


def test_goal_mutation_lock_is_scoped_to_one_session(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    judge_started = threading.Event()
    release_judge = threading.Event()
    other_session_done = threading.Event()

    def _blocked_judge(*_args, **_kwargs):
        judge_started.set()
        assert release_judge.wait(2), "test did not release the blocked judge"
        return "continue", "still working", False, None, False

    first = goals.GoalManager("session-first", default_max_turns=4)
    first.set("First goal")
    second = goals.GoalManager("session-second", default_max_turns=4)

    monkeypatch.setattr(goals, "judge_goal", _blocked_judge)
    evaluating = threading.Thread(
        target=first.evaluate_after_turn,
        args=("first response",),
        daemon=True,
    )
    mutating = threading.Thread(
        target=lambda: (second.set("Second goal"), other_session_done.set()),
        daemon=True,
    )

    evaluating.start()
    assert judge_started.wait(1), "first-session judge did not start"
    mutating.start()
    try:
        assert other_session_done.wait(0.25), (
            "an independent session was blocked by the first session's goal lock"
        )
    finally:
        release_judge.set()
        evaluating.join(2)
        mutating.join(2)

    assert goals.load_goal("session-second").goal == "Second goal"
