"""Tests for tools/skill_usage.py outcome telemetry + utility scoring (2026-08)."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir (mirrors test_skill_usage.py)."""
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


def _usage_file(home: Path) -> Path:
    return home / "skills" / ".usage.json"


def test_record_outcome_unknown_below_floor(skills_home):
    """One or two outcomes → utility stays None (below confidence floor)."""
    from tools.skill_usage import record_outcome, get_utility_score

    assert record_outcome("demo-skill", "success") is None
    assert record_outcome("demo-skill", "success") is None
    assert get_utility_score("demo-skill") is None


def test_record_outcome_utility_after_floor(skills_home):
    """3+ outcomes → utility score computed; all-success is high."""
    from tools.skill_usage import record_outcome, get_utility_score

    record_outcome("demo-skill", "success")
    record_outcome("demo-skill", "success")
    score = record_outcome("demo-skill", "success")
    assert score is not None
    assert score > 0.8
    assert get_utility_score("demo-skill") == score


def test_record_outcome_failures_lower_utility(skills_home):
    """Failure-heavy skill gets a low utility score."""
    from tools.skill_usage import record_outcome, get_utility_score

    for _ in range(3):
        record_outcome("demo-skill", "failure", error_type="api_403")
    score = get_utility_score("demo-skill")
    assert score is not None
    assert score < 0.2


def test_outcome_ring_buffer_bounded(skills_home):
    """Outcome history is bounded; old entries dropped, counters still correct."""
    from tools.skill_usage import record_outcome, get_outcome_summary

    for _ in range(60):
        record_outcome("demo-skill", "success")
    summary = get_outcome_summary("demo-skill")
    assert len(summary["outcomes"]) <= 50  # bounded
    # Counters derived from the ring buffer, not unbounded history
    assert summary["success_count"] == len(summary["outcomes"])


def test_get_outcome_summary_fields(skills_home):
    """Summary exposes skill/counters/utility/outcomes."""
    from tools.skill_usage import record_outcome, get_outcome_summary

    record_outcome("demo-skill", "success", task_id="t1")
    record_outcome("demo-skill", "failure", task_id="t2", error_type="timeout")
    s = get_outcome_summary("demo-skill")
    assert s["skill"] == "demo-skill"
    assert s["success_count"] == 1
    assert s["failure_count"] == 1
    assert s["utility_score"] is None  # below floor
    assert s["last_outcome_at"] is not None
    assert s["outcomes"][0]["error_type"] == "timeout"


def test_record_outcome_persisted_to_disk(skills_home):
    """Outcomes survive a reload (sidecar JSON persisted)."""
    from tools.skill_usage import record_outcome
    import tools.skill_usage as mod
    import importlib

    record_outcome("demo-skill", "success")
    record_outcome("demo-skill", "failure")
    record_outcome("demo-skill", "success")
    # Reload module to force re-read from disk
    importlib.reload(mod)
    s = mod.get_outcome_summary("demo-skill")
    assert s["success_count"] == 2
    assert s["failure_count"] == 1
    assert s["utility_score"] is not None


def test_invalid_outcome_rejected(skills_home):
    """Invalid outcome strings are rejected without side effects."""
    from tools.skill_usage import record_outcome, get_outcome_summary

    assert record_outcome("demo-skill", "banana") is None
    s = get_outcome_summary("demo-skill")
    assert s["success_count"] == 0
    assert s["failure_count"] == 0


def test_empty_skill_name_rejected(skills_home):
    from tools.skill_usage import record_outcome

    assert record_outcome("", "success") is None


def test_list_low_utility_skills(skills_home):
    """Only failure-heavy skills with enough samples are listed."""
    from tools.skill_usage import record_outcome, list_low_utility_skills

    # Good skill: 3 successes
    record_outcome("good-skill", "success")
    record_outcome("good-skill", "success")
    record_outcome("good-skill", "success")
    # Bad skill: 3 failures
    record_outcome("bad-skill", "failure")
    record_outcome("bad-skill", "failure")
    record_outcome("bad-skill", "failure")
    # Sparse skill: 2 failures — below floor, must NOT be listed
    record_outcome("sparse-skill", "failure")
    record_outcome("sparse-skill", "failure")

    rows = list_low_utility_skills()
    names = [r["skill"] for r in rows]
    assert "bad-skill" in names
    assert "good-skill" not in names
    assert "sparse-skill" not in names
    assert rows[0]["utility_score"] < 0.2


def test_utility_rerank_prefers_scored(skills_home):
    """Scored skills sort before no-signal skills; best utility first."""
    from tools.skill_usage import record_outcome, utility_rerank

    record_outcome("skill-b", "success")
    record_outcome("skill-b", "success")
    record_outcome("skill-b", "success")  # high utility
    record_outcome("skill-a", "failure")
    record_outcome("skill-a", "failure")
    record_outcome("skill-a", "failure")  # low utility

    # Original order: [skill-a, skill-c, skill-b]
    result = utility_rerank(["skill-a", "skill-c", "skill-b"])
    # skill-b (high) first, skill-a (low) next, skill-c (no signal) last
    assert result[0] == "skill-b"
    assert result[1] == "skill-a"
    assert result[2] == "skill-c"


def test_utility_rerank_preserves_order_when_no_signal(skills_home):
    """Without telemetry, re-rank is a stable no-op."""
    from tools.skill_usage import utility_rerank

    assert utility_rerank(["x", "y", "z"]) == ["x", "y", "z"]
    assert utility_rerank([]) == []
