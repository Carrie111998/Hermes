"""Tests for /goal quality gates (GoalGate, run_gate, GoalManager gate flow)."""

import json
import time
from unittest.mock import patch

import pytest

from hermes_cli.goals import (
    DEFAULT_GATE_MAX_RETRIES,
    DEFAULT_GATE_TIMEOUT_SECONDS,
    GoalGate,
    GoalManager,
    GoalState,
    run_gate,
    save_goal,
    load_goal,
)


# ──────────────────────────────────────────────────────────────────────
# GoalGate serialization
# ──────────────────────────────────────────────────────────────────────


def test_gate_roundtrip_through_goalstate_json():
    state = GoalState(goal="ship it", status="active")
    state.gates.append(GoalGate(command="echo ok", timeout_seconds=42, max_retries=7))
    raw = state.to_json()
    loaded = GoalState.from_json(raw)
    assert len(loaded.gates) == 1
    g = loaded.gates[0]
    assert g.command == "echo ok"
    assert g.timeout_seconds == 42
    assert g.max_retries == 7
    assert g.attempts == 0


def test_gate_from_dict_defaults_and_garbage():
    g = GoalGate.from_dict({"command": "true"})
    assert g.timeout_seconds == DEFAULT_GATE_TIMEOUT_SECONDS
    assert g.max_retries == DEFAULT_GATE_MAX_RETRIES
    assert GoalGate.from_dict(None).command == ""
    assert GoalGate.from_dict("nonsense").command == ""


def test_old_state_rows_without_gates_load_clean():
    """Backwards compatibility: pre-gates state_meta rows load with no gates."""
    old = {"goal": "legacy", "status": "active"}
    state = GoalState.from_json(json.dumps(old))
    assert state.gates == []


# ──────────────────────────────────────────────────────────────────────
# run_gate
# ──────────────────────────────────────────────────────────────────────


def test_run_gate_pass():
    passed, code, out = run_gate(GoalGate(command="echo hello"))
    assert passed is True
    assert code == 0
    assert "hello" in out


def test_run_gate_fail_captures_output():
    passed, code, out = run_gate(GoalGate(command="echo broken >&2; exit 3"))
    assert passed is False
    assert code == 3
    assert "broken" in out


def test_run_gate_timeout():
    passed, code, out = run_gate(GoalGate(command="sleep 5", timeout_seconds=1))
    assert passed is False
    assert code == -1
    assert "timed out" in out


# ──────────────────────────────────────────────────────────────────────
# GoalManager gate management
# ──────────────────────────────────────────────────────────────────────


def _mgr_with_goal(session_id="gate-test-sid"):
    mgr = GoalManager(session_id=session_id)
    mgr.set("test goal")
    return mgr


def test_add_remove_clear_gates():
    mgr = _mgr_with_goal("gate-mgmt-sid")
    mgr.add_gate("echo one")
    mgr.add_gate("echo two")
    assert len(mgr.state.gates) == 2
    assert "echo one" in mgr.render_gates()

    removed = mgr.remove_gate(1)
    assert removed == "echo one"
    assert len(mgr.state.gates) == 1

    assert mgr.clear_gates() == 1
    assert mgr.state.gates == []


def test_add_gate_requires_active_goal():
    mgr = GoalManager(session_id="gate-nogoal-sid")
    with pytest.raises(RuntimeError):
        mgr.add_gate("echo nope")


def test_gates_persist_and_reload():
    mgr = _mgr_with_goal("gate-persist-sid")
    mgr.add_gate("echo persisted")
    reloaded = GoalManager(session_id="gate-persist-sid")
    assert len(reloaded.state.gates) == 1
    assert reloaded.state.gates[0].command == "echo persisted"


def test_status_line_mentions_gates():
    mgr = _mgr_with_goal("gate-status-sid")
    mgr.add_gate("echo g")
    assert "1 gate" in mgr.status_line()


# ──────────────────────────────────────────────────────────────────────
# evaluate_after_turn integration
# ──────────────────────────────────────────────────────────────────────


def _evaluate_model_outcome(mgr, outcome, reason="model outcome", turn_id="turn-1"):
    generation_id = mgr.begin_model_turn(turn_id)
    assert generation_id
    assert mgr.record_model_outcome(
        outcome,
        reason,
        originating_turn_id=turn_id,
        goal_generation_id=generation_id,
    )
    return mgr.evaluate_after_turn(
        "prose is not interpreted",
        originating_turn_id=turn_id,
        goal_generation_id=generation_id,
    )


def test_failing_gate_verifies_model_authored_completion():
    mgr = _mgr_with_goal("gate-fail-sid")
    mgr.add_gate("exit 5")
    with patch("hermes_cli.goals.workspace_fingerprint", return_value=""):
        decision = _evaluate_model_outcome(mgr, "complete")
    assert decision["verdict"] == "gate_failed"
    assert decision["should_continue"] is True
    assert "exit 5" in decision["continuation_prompt"]
    assert "quality gate" in decision["continuation_prompt"].lower()


def test_passing_gates_accept_model_authored_completion():
    mgr = _mgr_with_goal("gate-pass-sid")
    mgr.add_gate("true")
    decision = _evaluate_model_outcome(mgr, "complete", "all good")
    assert decision["verdict"] == "done"
    # Passing run resets attempt bookkeeping.
    assert mgr.state.gates[0].attempts == 0
    assert mgr.state.gates[0].last_exit_code == 0


def test_gate_retry_exhaustion_pauses_goal():
    mgr = _mgr_with_goal("gate-exhaust-sid")
    mgr.add_gate("exit 1", max_retries=2)
    with patch("hermes_cli.goals.workspace_fingerprint", return_value=""):
        d1 = _evaluate_model_outcome(mgr, "complete", turn_id="turn-1")
        d2 = _evaluate_model_outcome(mgr, "complete", turn_id="turn-2")
        d3 = _evaluate_model_outcome(mgr, "complete", turn_id="turn-3")
    assert d1["should_continue"] is True
    assert d2["should_continue"] is True
    assert d3["status"] == "paused"
    assert d3["should_continue"] is False
    assert mgr.state.status == "paused"
    assert "gate" in (mgr.state.paused_reason or "")


def test_unchanged_workspace_skips_rerun():
    mgr = _mgr_with_goal("gate-unchanged-sid")
    mgr.add_gate("exit 1")
    with patch("hermes_cli.goals.workspace_fingerprint", return_value="fp-1"):
        _evaluate_model_outcome(mgr, "complete", turn_id="turn-1")
        # Second turn, same fingerprint — run_gate must NOT run again.
        with patch("hermes_cli.goals.run_gate") as mock_run:
            d2 = _evaluate_model_outcome(mgr, "complete", turn_id="turn-2")
        mock_run.assert_not_called()
    assert d2["verdict"] == "gate_failed"
    assert "unchanged" in d2["message"]


def test_changed_workspace_reruns_gate():
    mgr = _mgr_with_goal("gate-changed-sid")
    mgr.add_gate("exit 1")
    with patch("hermes_cli.goals.workspace_fingerprint", return_value="fp-1"):
        _evaluate_model_outcome(mgr, "complete", turn_id="turn-1")
    with patch("hermes_cli.goals.workspace_fingerprint", return_value="fp-2"), \
         patch("hermes_cli.goals.run_gate", return_value=(False, 1, "still red")) as mock_run:
        _evaluate_model_outcome(mgr, "complete", turn_id="turn-2")
    mock_run.assert_called_once()


def test_gate_continuation_respects_turn_budget():
    mgr = GoalManager(session_id="gate-budget-sid", default_max_turns=1)
    mgr.set("budget goal")
    mgr.add_gate("exit 1")
    with patch("hermes_cli.goals.workspace_fingerprint", return_value=""):
        decision = _evaluate_model_outcome(mgr, "complete", turn_id="only-turn")
    assert decision["status"] == "paused"
    assert decision["should_continue"] is False
    assert "turns used" in decision["message"]


def test_no_gates_behaves_exactly_as_before():
    mgr = _mgr_with_goal("gate-none-sid")
    decision = _evaluate_model_outcome(mgr, "continue", "keep going")
    assert decision["verdict"] == "continue"
    assert decision["should_continue"] is True
