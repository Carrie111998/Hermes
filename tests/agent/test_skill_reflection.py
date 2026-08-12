"""Tests for agent/skill_reflection.py — failure-driven skill reflection loop."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean skills/ dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_skill(skills_dir: Path, name: str):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: test skill
---

# {name}

## When to Use
- trigger one

## Procedure
1. step one
2. step two

## Pitfalls
- nothing yet

## Verification
- run check
""",
        encoding="utf-8",
    )
    return d


def _proposals_dir(home: Path) -> Path:
    return home / "skills" / ".reflection_proposals"


def test_locate_section_found():
    from agent.skill_reflection import locate_section

    md = "# Demo\n\n## Pitfalls\n- api 403\n\n## Verification\n- run\n"
    loc = locate_section(md, "pitfalls")
    assert loc is not None
    assert loc["heading"] == "## Pitfalls"
    assert md[loc["start"]:loc["end"]].startswith("## Pitfalls")


def test_locate_section_missing():
    from agent.skill_reflection import locate_section

    assert locate_section("# Demo\n", "verification") is None
    assert locate_section("", "pitfalls") is None
    assert locate_section("# Demo\n## Pitfalls\n", "bogus-section") is None


def test_build_proposal_valid():
    from agent.skill_reflection import build_proposal

    p = build_proposal(
        "demo-skill",
        diagnosis="API returns 403 after token expiry",
        target_section="pitfalls",
        suggested_fix="Add a note about token refresh before calls",
        failure_types=["api_403"],
        utility_score=0.1,
    )
    assert p["skill"] == "demo-skill"
    assert p["target_section"] == "pitfalls"
    assert p["heading"] == "## Pitfalls"
    assert p["status"] == "pending"
    assert p["utility_score"] == 0.1
    assert "api_403" in p["failure_types"]


def test_build_proposal_invalid_target():
    from agent.skill_reflection import build_proposal

    with pytest.raises(ValueError):
        build_proposal("demo-skill", diagnosis="d", target_section="nope", suggested_fix="f")


def test_save_and_list_proposals(hermes_home):
    from agent.skill_reflection import build_proposal, save_proposal, list_proposals

    p = build_proposal("demo-skill", diagnosis="d", target_section="pitfalls", suggested_fix="f")
    path = save_proposal(p)
    assert path is not None
    assert path.exists()
    rows = list_proposals()
    assert len(rows) == 1
    assert rows[0]["skill"] == "demo-skill"
    assert rows[0]["status"] == "pending"


def test_save_proposal_requires_id(hermes_home):
    from agent.skill_reflection import save_proposal

    assert save_proposal({"skill": "x"}) is None  # missing proposal_id
    assert save_proposal({"skill": "", "proposal_id": "abc"}) is None


def test_list_pending_and_mark_status(hermes_home):
    from agent.skill_reflection import (
        build_proposal,
        save_proposal,
        list_pending_proposals,
        mark_proposal_status,
        list_proposals,
    )

    p = build_proposal("demo-skill", diagnosis="d", target_section="procedure", suggested_fix="f")
    save_proposal(p)
    assert len(list_pending_proposals("demo-skill")) == 1
    # Reject keeps it for audit
    assert mark_proposal_status("demo-skill", p["proposal_id"], "rejected")
    assert len(list_pending_proposals("demo-skill")) == 0
    assert len(list_proposals("demo-skill")) == 1
    assert list_proposals("demo-skill")[0]["status"] == "rejected"
    # Apply removes from queue
    p2 = build_proposal("demo-skill", diagnosis="d2", target_section="procedure", suggested_fix="f2")
    save_proposal(p2)
    assert mark_proposal_status("demo-skill", p2["proposal_id"], "applied")
    remaining = [x for x in list_proposals("demo-skill") if x["status"] == "pending"]
    assert len(remaining) == 0


def test_mark_status_invalid(hermes_home):
    from agent.skill_reflection import mark_proposal_status

    assert not mark_proposal_status("demo-skill", "nope", "bogus-status")


def test_proposals_queue_bounded(hermes_home):
    from agent.skill_reflection import build_proposal, save_proposal, list_proposals

    for i in range(8):
        p = build_proposal(
            "demo-skill",
            diagnosis=f"failure {i}",
            target_section="pitfalls",
            suggested_fix=f"fix {i}",
        )
        save_proposal(p)
    rows = list_proposals("demo-skill")
    # Bound is MAX_PROPOSALS_PER_SKILL = 20; rejected/applied may linger, so
    # total rows can exceed, but pending must never exceed the bound.
    from agent.skill_reflection import MAX_PROPOSALS_PER_SKILL
    pending = [r for r in rows if r.get("status") == "pending"]
    assert len(pending) <= MAX_PROPOSALS_PER_SKILL


def test_aggregate_failure_patterns(skills_home_factory):
    home = skills_home_factory
    # Seed outcome telemetry for the skill
    from tools.skill_usage import record_outcome
    from agent.skill_reflection import aggregate_failure_patterns

    record_outcome("demo-skill", "failure", error_type="api_403")
    record_outcome("demo-skill", "failure", error_type="api_403")
    record_outcome("demo-skill", "failure", error_type="timeout")
    record_outcome("demo-skill", "success")

    pat = aggregate_failure_patterns("demo-skill")
    assert pat["skill"] == "demo-skill"
    assert pat["failure_count"] == 3
    assert pat["success_count"] == 1
    assert pat["failure_rate"] == 0.75
    top = dict(pat["top_error_types"])
    assert top.get("api_403") == 2


@pytest.fixture
def skills_home_factory(tmp_path, monkeypatch):
    """HERMES_HOME sandbox shared with skill_usage outcome tests."""
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


def test_reflection_candidates(skills_home_factory):
    from tools.skill_usage import record_outcome
    from agent.skill_reflection import reflection_candidates

    # Failing skill
    record_outcome("bad-skill", "failure", error_type="api_403")
    record_outcome("bad-skill", "failure", error_type="api_403")
    record_outcome("bad-skill", "failure", error_type="timeout")
    # Working skill
    record_outcome("good-skill", "success")
    record_outcome("good-skill", "success")
    record_outcome("good-skill", "success")

    cands = reflection_candidates()
    names = [c["skill"] for c in cands]
    assert "bad-skill" in names
    assert "good-skill" not in names
    bad = next(c for c in cands if c["skill"] == "bad-skill")
    assert bad["failure_count"] == 3
    assert bad["top_error_types"][0][0] == "api_403"
