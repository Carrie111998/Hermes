"""Native persistent-goal coverage for direct API-server agent turns."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.api_goal_runtime import run_goal_aware_turn
from hermes_cli import goals


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    token = set_hermes_home_override(str(home))
    goals._DB_CACHE.clear()
    yield home
    reset_hermes_home_override(token)
    goals._DB_CACHE.clear()


def test_api_goal_runs_canonical_continuations_until_done(hermes_home):
    calls = []
    events = []

    def run_turn(message, history):
        calls.append((message, history))
        return {"final_response": f"turn {len(calls)}", "completed": True}

    verdicts = [
        ("continue", "one step remains", False, None, False),
        ("done", "objective complete", False, None, False),
    ]
    with (
        patch("hermes_cli.goals.judge_goal", side_effect=verdicts),
        patch("hermes_cli.goals.gather_background_processes", return_value=[]),
    ):
        result = run_goal_aware_turn(
            session_id="api-goal-session",
            user_message="/goal finish the migration",
            conversation_history=[{"role": "user", "content": "prior"}],
            run_turn=run_turn,
            default_max_turns=4,
            status_callback=events.append,
        )

    assert result["final_response"] == "turn 2"
    assert calls[0] == (
        "finish the migration",
        [{"role": "user", "content": "prior"}],
    )
    assert calls[1][0].startswith("[Continuing toward your standing goal]")
    assert calls[1][1] is None
    assert [event["event"] for event in events] == [
        "goal.started",
        "goal.status",
        "goal.status",
    ]
    assert goals.GoalManager("api-goal-session").state.status == "done"


def test_api_goal_wait_parks_without_another_turn(hermes_home):
    calls = []

    def run_turn(message, history):
        calls.append((message, history))
        return {"final_response": "The build watcher is still running."}

    wait_verdict = (
        "wait",
        "waiting for the build watcher",
        False,
        {"seconds": 60},
        False,
    )
    with (
        patch("hermes_cli.goals.judge_goal", return_value=wait_verdict),
        patch("hermes_cli.goals.gather_background_processes", return_value=[]),
    ):
        run_goal_aware_turn(
            session_id="api-wait-session",
            user_message="/goal monitor the build until it finishes",
            conversation_history=None,
            run_turn=run_turn,
            status_callback=lambda _event: None,
        )

    assert len(calls) == 1
    state = goals.GoalManager("api-wait-session").state
    assert state.status == "active"
    assert state.waiting_until > 0
    assert state.turns_used == 1


def test_api_goal_reentry_resumes_after_wait_barrier_clears(hermes_home):
    calls = []

    def run_turn(message, history):
        calls.append((message, history))
        return {"final_response": f"turn {len(calls)}"}

    with (
        patch(
            "hermes_cli.goals.judge_goal",
            side_effect=[
                ("wait", "build is running", False, {"seconds": 60}, False),
                ("done", "build completed", False, None, False),
            ],
        ),
        patch("hermes_cli.goals.gather_background_processes", return_value=[]),
    ):
        run_goal_aware_turn(
            session_id="api-wake-session",
            user_message="/goal monitor the build",
            conversation_history=None,
            run_turn=run_turn,
        )

        manager = goals.GoalManager("api-wake-session")
        manager.state.waiting_until = 0
        goals.save_goal("api-wake-session", manager.state)

        result = run_goal_aware_turn(
            session_id="api-wake-session",
            user_message="The background build completed successfully.",
            conversation_history=None,
            run_turn=run_turn,
        )

    assert result["final_response"] == "turn 2"
    assert len(calls) == 2
    assert goals.GoalManager("api-wake-session").state.status == "done"


def test_api_goal_honors_control_plane_pause_during_model_turn(hermes_home):
    judge_called = False

    def run_turn(_message, _history):
        goals.GoalManager("api-pause-session").pause()
        return {"final_response": "late response from the in-flight turn"}

    def forbidden_judge(*_args, **_kwargs):
        nonlocal judge_called
        judge_called = True
        raise AssertionError("a paused goal must not be judged or continued")

    with patch("hermes_cli.goals.judge_goal", side_effect=forbidden_judge):
        result = run_goal_aware_turn(
            session_id="api-pause-session",
            user_message="/goal complete the build",
            conversation_history=None,
            run_turn=run_turn,
        )

    assert result["final_response"] == "late response from the in-flight turn"
    assert judge_called is False
    assert goals.GoalManager("api-pause-session").state.status == "paused"


def test_api_goal_stop_predicate_prevents_next_continuation(hermes_home):
    calls = []
    stopping = False

    def run_turn(message, history):
        nonlocal stopping
        calls.append((message, history))
        stopping = True
        return {"final_response": "first turn completed during stop"}

    with patch(
        "hermes_cli.goals.judge_goal",
        side_effect=AssertionError("stopping run must not call the goal judge"),
    ):
        run_goal_aware_turn(
            session_id="api-stop-session",
            user_message="/goal complete a long task",
            conversation_history=None,
            run_turn=run_turn,
            should_stop=lambda: stopping,
        )

    assert len(calls) == 1
    assert goals.GoalManager("api-stop-session").state.status == "active"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/goal", "No active goal"),
        ("/goal status", "No active goal"),
        ("/goal pause", "No active goal"),
        ("/goal clear", "Goal cleared"),
        ("/goal resume", "No goal to resume"),
        ("/goal unwait", "no active wait"),
        ("/goal wait 123 build", "Could not park goal"),
    ],
)
def test_api_goal_control_commands_do_not_call_model(hermes_home, command, expected):
    def forbidden_turn(_message, _history):
        raise AssertionError("goal control commands must not call the model")

    result = run_goal_aware_turn(
        session_id=f"control-{command}",
        user_message=command,
        conversation_history=None,
        run_turn=forbidden_turn,
    )

    assert expected in result["final_response"]
    assert result["goal_control"] is True
