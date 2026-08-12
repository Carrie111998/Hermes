"""Tests for tools/skills_tool.skill_report_outcome — the agent-facing skill
effectiveness reporter (feeds the evolution dashboard)."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
    return home


def test_report_success(skills_home):
    from tools.skills_tool import _skill_report_outcome

    r = json.loads(_skill_report_outcome({"name": "demo", "outcome": "success"}, task_id="t1"))
    assert r["success"] is True
    assert r["skill"] == "demo"
    assert r["outcome"] == "success"


def test_report_failure_with_error_type(skills_home):
    from tools.skills_tool import _skill_report_outcome

    r = json.loads(
        _skill_report_outcome(
            {"name": "demo", "outcome": "failure", "error_type": "api_403"}, task_id="t1"
        )
    )
    assert r["success"] is True
    assert r["outcome"] == "failure"

    # Verify it landed in the telemetry
    from tools.skill_usage import get_outcome_summary
    s = get_outcome_summary("demo")
    assert s["failure_count"] == 1
    assert s["outcomes"][0]["error_type"] == "api_403"


def test_report_invalid_outcome(skills_home):
    from tools.skills_tool import _skill_report_outcome

    r = json.loads(_skill_report_outcome({"name": "demo", "outcome": "banana"}))
    assert r["success"] is False
    assert "required" in r["error"]


def test_report_missing_name(skills_home):
    from tools.skills_tool import _skill_report_outcome

    r = json.loads(_skill_report_outcome({"outcome": "success"}))
    assert r["success"] is False


def test_report_three_outcomes_yields_utility(skills_home):
    from tools.skills_tool import _skill_report_outcome

    utility = None
    for i in range(3):
        r = json.loads(_skill_report_outcome({"name": "demo", "outcome": "success"}, task_id=f"t{i}"))
        utility = r["utility_score"]
    assert utility is not None
    assert utility > 0.8  # 3 successes → high utility


def test_report_best_effort_no_crash(skills_home, monkeypatch):
    """Telemetry failure never breaks the tool call."""
    from tools.skills_tool import _skill_report_outcome

    import tools.skill_usage as su
    monkeypatch.setattr(su, "record_outcome", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # Re-import the handler's module-level import path is dynamic, so patch the source module
    import tools.skills_tool as st
    r = json.loads(st._skill_report_outcome({"name": "demo", "outcome": "success"}))
    assert r["success"] is False  # error surfaced, no raise
