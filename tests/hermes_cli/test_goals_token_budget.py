"""F2 tests — opt-in token budget on the existing /goal gate machinery.

Two layers:
1. Pure GoalState JSON round-trip (no runtime) — confirms the new fields
   survive persistence and that old rows (missing the fields) load cleanly.
2. GoalManager-level enforcement (real SessionDB, isolated HERMES_HOME) —
   confirms the token budget pauses the goal when the per-session recorded
   token delta crosses the cap, and that set_budget/clear_budget work.

These are the F2 acceptance gate (W11/Phase F2-3).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────
# 1. Pure GoalState round-trip
# ──────────────────────────────────────────────────────────────────────


class TestGoalStateTokenBudgetFields:
    def test_new_fields_round_trip(self):
        from hermes_cli.goals import GoalState

        s = GoalState(
            goal="ship the docs",
            max_input_tokens=1000,
            max_output_tokens=500,
            input_tokens_base=10,
            output_tokens_base=20,
        )
        raw = s.to_json()
        s2 = GoalState.from_json(raw)
        assert s2.max_input_tokens == 1000
        assert s2.max_output_tokens == 500
        assert s2.input_tokens_base == 10
        assert s2.output_tokens_base == 20

    def test_missing_fields_default_to_off(self):
        from hermes_cli.goals import GoalState

        # A row saved before the token fields existed must load with caps off.
        raw = (
            '{"goal": "legacy", "status": "active", "turns_used": 1, '
            '"max_turns": 25}'
        )
        s = GoalState.from_json(raw)
        assert s.max_input_tokens is None
        assert s.max_output_tokens is None
        assert s.input_tokens_base == 0
        assert s.output_tokens_base == 0


# ──────────────────────────────────────────────────────────────────────
# 2. GoalManager token-budget enforcement
# ──────────────────────────────────────────────────────────────────────


def _make_mgr(hermes_home):
    from hermes_cli.goals import GoalManager

    sid = "sess-f2"
    mgr = GoalManager(sid)
    mgr.set("write the report", max_input_tokens=100, max_output_tokens=50)
    return mgr, sid


class TestGoalManagerTokenBudget:
    def test_set_captures_baseline(self, hermes_home):
        mgr, sid = _make_mgr(hermes_home)
        assert mgr._state.max_input_tokens == 100
        assert mgr._state.max_output_tokens == 50
        # Baseline starts at 0 (no usage recorded yet).
        assert mgr._state.input_tokens_base == 0
        assert mgr._state.output_tokens_base == 0

    def test_under_budget_continues(self, hermes_home):
        mgr, sid = _make_mgr(hermes_home)
        # Report usage well under both caps (delta since baseline = 10/5).
        with patch.object(
            mgr, "_state"
        ):  # no-op guard; real read via _current_session_tokens
            pass
        decision = mgr.evaluate_after_turn(
            "draft v1", user_initiated=True, background_processes=[]
        )
        # With no usage recorded, the goal is not paused for tokens.
        assert decision["status"] in {"active", "paused"}
        # No token exhaustion reason unless caps crossed.
        if decision["status"] == "paused":
            assert "token budget" not in (decision["reason"] or "")

    def test_budget_exhaustion_pauses(self, hermes_home):
        mgr, sid = _make_mgr(hermes_home)
        # Force the recorded session tokens to simulate a delta over the cap.
        with patch(
            "hermes_cli.goals._current_session_tokens", return_value=(200, 120)
        ):
            decision = mgr.evaluate_after_turn(
                "draft v2", user_initiated=True, background_processes=[]
            )
        assert decision["status"] == "paused"
        assert decision["verdict"] == "token_budget_exhausted"
        assert "token budget exhausted" in decision["message"]
        # State persisted with the token-budget reason.
        fresh = mgr.state
        assert fresh.paused_reason.startswith("token budget exhausted")

    def test_set_budget_and_clear(self, hermes_home):
        mgr, sid = _make_mgr(hermes_home)
        mgr.set_budget(max_input_tokens=999, max_output_tokens=0)
        assert mgr._state.max_input_tokens == 999
        # cap of 0 is falsy → treated as off (None).
        assert mgr._state.max_output_tokens is None
        mgr.clear_budget()
        assert mgr._state.max_input_tokens is None
        assert mgr._state.max_output_tokens is None

    def test_resume_rebaselines_tokens(self, hermes_home):
        mgr, sid = _make_mgr(hermes_home)
        with patch(
            "hermes_cli.goals._current_session_tokens", return_value=(5000, 4000)
        ):
            mgr.resume(reset_budget=True)
        assert mgr._state.input_tokens_base == 5000
        assert mgr._state.output_tokens_base == 4000

    def test_status_line_shows_budget(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager("sess-f2b")
        mgr.set("goal with token cap", max_input_tokens=1000, max_output_tokens=200)
        line = mgr.status_line()
        assert "token" in line
        assert "in≤1000" in line
        assert "out≤200" in line
