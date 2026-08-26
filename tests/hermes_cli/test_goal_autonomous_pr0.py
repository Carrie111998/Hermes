"""Tests for PR0: /goal file-loading + autonomous mode (hermes_cli/goals.py).

Covers the additive surfaces PR0 introduces on top of the native Ralph-loop
/goal:
  * ``resolve_goal_input`` — parses ``--auto`` / ``--autonomous`` flags and
    loads a GOAL.md-style file when the argument is a single path token;
  * ``GoalState.autonomous`` — persists through to_json/from_json and is
    back-compatible with pre-PR0 state rows that lack the field;
  * ``GoalManager.set(autonomous=...)`` — records the flag on the state.

All offline; no judge calls, no network.
"""

from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------- #
# resolve_goal_input: flag parsing                                            #
# --------------------------------------------------------------------------- #
class TestResolveGoalInputFlags:
    def test_plain_goal_is_unchanged_and_not_autonomous(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, note = resolve_goal_input("fix the parser")
        assert text == "fix the parser"
        assert autonomous is False
        assert note == ""

    def test_leading_auto_flag_sets_autonomous_and_is_stripped(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input("--auto ship the widget")
        assert text == "ship the widget"
        assert autonomous is True

    def test_trailing_auto_flag_also_works(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input("ship the widget --auto")
        assert text == "ship the widget"
        assert autonomous is True

    @pytest.mark.parametrize("keyword", ["auto", "autonomous"])
    def test_bare_keyword_only_enables_when_it_is_the_entire_argument(self, keyword):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input(keyword)
        assert text == ""
        assert autonomous is True

    def test_autonomous_goal_prose_is_literal_and_non_autonomous(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, note = resolve_goal_input(
            "autonomous drone surveillance pipeline"
        )
        assert text == "autonomous drone surveillance pipeline"
        assert autonomous is False
        assert note == ""

    def test_auto_goal_prose_is_literal_and_non_autonomous(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, note = resolve_goal_input("auto remediation workflow")
        assert text == "auto remediation workflow"
        assert autonomous is False
        assert note == ""

    def test_non_leading_bare_autonomous_is_NOT_a_switch(self):
        # Regression: a bare keyword that is not the leading token stays part of
        # the goal — "ship the autonomous drone" keeps every word and is not
        # autonomous. Only a LEADING bare keyword (or a dashed flag) toggles.
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input("ship the autonomous drone")
        assert text == "ship the autonomous drone"
        assert autonomous is False

    def test_non_leading_bare_auto_trailing_is_NOT_a_switch(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input("wire the relay to auto")
        assert text == "wire the relay to auto"
        assert autonomous is False

    def test_autonomous_default_seeds_flag_without_explicit_switch(self):
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, _ = resolve_goal_input(
            "do the thing", autonomous_default=True
        )
        assert text == "do the thing"
        assert autonomous is True

    def test_colon_in_prose_goal_is_not_treated_as_a_path(self):
        # A normal goal with an incidental colon/word must not be mistaken for
        # a file path (guards the file heuristic's conservatism).
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, note = resolve_goal_input("Fix bug: the parser")
        assert text == "Fix bug: the parser"
        assert autonomous is False
        assert note == ""


# --------------------------------------------------------------------------- #
# resolve_goal_input: GOAL.md file loading                                    #
# --------------------------------------------------------------------------- #
class TestResolveGoalInputFile:
    def test_loads_goal_from_absolute_file(self, tmp_path):
        from hermes_cli.goals import resolve_goal_input

        goal_file = tmp_path / "GOAL.md"
        goal_file.write_text("Migrate auth to JWT\nverify: the auth suite passes\n")
        text, autonomous, note = resolve_goal_input(str(goal_file))
        assert "Migrate auth to JWT" in text
        assert "verify: the auth suite passes" in text
        assert autonomous is False
        assert "loaded goal from" in note

    def test_loads_relative_file_against_cwd(self, tmp_path):
        from hermes_cli.goals import resolve_goal_input

        (tmp_path / "GOAL.md").write_text("do the relative thing")
        text, _autonomous, note = resolve_goal_input("GOAL.md", cwd=str(tmp_path))
        assert text == "do the relative thing"
        assert "loaded goal from" in note

    def test_file_plus_auto_flag_loads_and_sets_autonomous(self, tmp_path):
        from hermes_cli.goals import resolve_goal_input

        (tmp_path / "GOAL.md").write_text("autonomous file goal")
        text, autonomous, note = resolve_goal_input(
            "--auto GOAL.md", cwd=str(tmp_path)
        )
        assert text == "autonomous file goal"
        assert autonomous is True
        assert "loaded goal from" in note

    def test_leading_keyword_plus_file_remains_literal_goal_prose(self, tmp_path):
        from hermes_cli.goals import resolve_goal_input

        (tmp_path / "GOAL.md").write_text("keyword file goal")
        text, autonomous, note = resolve_goal_input(
            "auto GOAL.md", cwd=str(tmp_path)
        )
        assert text == "auto GOAL.md"
        assert autonomous is False
        assert note == ""

    def test_missing_file_path_falls_back_to_literal_text(self):
        # A path-looking token that does not resolve to a file is treated as
        # literal goal text (fail-open, never raises), and the note diagnoses
        # the miss so a typo like GOL.md is visible rather than silent.
        from hermes_cli.goals import resolve_goal_input

        text, autonomous, note = resolve_goal_input("./nonexistent-goal.md")
        assert text == "./nonexistent-goal.md"
        assert autonomous is False
        assert "no such file" in note.lower()

    def test_oversized_file_is_not_loaded(self, tmp_path):
        from hermes_cli.goals import resolve_goal_input

        big = tmp_path / "HUGE.md"
        big.write_text("x" * (64 * 1024 + 10))
        text, _autonomous, note = resolve_goal_input(str(big))
        # Falls back to the literal token; the giant body is never inlined, and
        # the note explains why (over the cap) rather than failing silently.
        assert text == str(big)
        assert "cap" in note.lower()


# --------------------------------------------------------------------------- #
# GoalState.autonomous persistence + back-compat                             #
# --------------------------------------------------------------------------- #
class TestGoalStateAutonomous:
    def test_autonomous_round_trips_through_json(self):
        from hermes_cli.goals import GoalState

        state = GoalState(goal="ship it", autonomous=True)
        restored = GoalState.from_json(state.to_json())
        assert restored.autonomous is True

    def test_default_autonomous_is_false(self):
        from hermes_cli.goals import GoalState

        assert GoalState(goal="ship it").autonomous is False

    def test_pre_pr0_row_without_autonomous_loads_as_false(self):
        """A goal serialized BEFORE the autonomous field existed must load with
        autonomous=False, not crash (back-compat, mirrors the subgoals case)."""
        from hermes_cli.goals import GoalState

        legacy = json.dumps({
            "goal": "do a thing",
            "status": "active",
            "turns_used": 2,
            "max_turns": 20,
            "created_at": 1.0,
            "last_turn_at": 2.0,
            "consecutive_parse_failures": 0,
        })
        state = GoalState.from_json(legacy)
        assert state.goal == "do a thing"
        assert state.autonomous is False


# --------------------------------------------------------------------------- #
# GoalManager.set persists the flag                                          #
# --------------------------------------------------------------------------- #
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


class TestGoalManagerSetAutonomous:
    def test_set_records_autonomous_and_persists(self, hermes_home):
        from hermes_cli.goals import GoalManager, load_goal

        mgr = GoalManager(session_id="auto-sid")
        state = mgr.set("ship the PR", autonomous=True)
        assert state.autonomous is True
        # Reloaded from the durable store, the flag survives.
        reloaded = load_goal("auto-sid")
        assert reloaded is not None and reloaded.autonomous is True

    def test_set_defaults_to_non_autonomous(self, hermes_home):
        from hermes_cli.goals import GoalManager

        mgr = GoalManager(session_id="plain-sid")
        state = mgr.set("ordinary goal")
        assert state.autonomous is False
