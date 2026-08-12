"""Tests for hermes_cli/web_routers/skill_evolution.py — the skill-evolution
dashboard API (outcome telemetry + reflection proposal queue + approval)."""

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
    import importlib
    import tools.skill_usage as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_prune_builtins_enabled", lambda: False)
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

## Pitfalls
- nothing yet

## Verification
- run check
""",
        encoding="utf-8",
    )
    return d


def _seed_telemetry(home: Path, skill: str, successes: int, failures: int, error_type: str | None = None):
    from tools.skill_usage import record_outcome
    for _ in range(successes):
        record_outcome(skill, "success", task_id="t")
    for _ in range(failures):
        record_outcome(skill, "failure", task_id="t", error_type=error_type)


def _seed_proposal(home: Path, skill: str, target: str = "pitfalls"):
    from agent.skill_reflection import build_proposal, save_proposal
    p = build_proposal(
        skill,
        diagnosis="API returns 403 after token expiry",
        target_section=target,
        suggested_fix="Add token-refresh note before calls",
        failure_types=["api_403"],
        utility_score=0.1,
    )
    return save_proposal(p)


def test_overview_empty(hermes_home):
    """Empty telemetry → overview returns zeroed stats, no crash."""
    from hermes_cli.web_routers.skill_evolution import skill_evolution_overview
    import asyncio

    data = asyncio.run(skill_evolution_overview())
    assert data["skills_total"] >= 0
    assert data["proposals_pending"] == 0
    assert data["proposals_total"] == 0
    assert data["avg_utility"] is None


def test_overview_with_telemetry(hermes_home):
    """Telemetry flows into overview stats."""
    from hermes_cli.web_routers.skill_evolution import skill_evolution_overview
    import asyncio

    _seed_telemetry(hermes_home, "good-skill", successes=3, failures=0)
    _seed_telemetry(hermes_home, "bad-skill", successes=0, failures=3, error_type="api_403")

    data = asyncio.run(skill_evolution_overview())
    assert data["skills_total"] >= 2
    assert data["skills_scored"] >= 2
    assert data["avg_utility"] is not None
    names = {c["skill"] for c in data["low_utility_candidates"]}
    assert "bad-skill" in names
    assert "good-skill" not in names


def test_outcomes_sorted_best_first(hermes_home):
    from hermes_cli.web_routers.skill_evolution import skill_evolution_outcomes
    import asyncio

    _seed_telemetry(hermes_home, "good-skill", successes=3, failures=0)
    _seed_telemetry(hermes_home, "bad-skill", successes=0, failures=3)

    data = asyncio.run(skill_evolution_outcomes(limit=50))
    skills = data["skills"]
    assert len(skills) >= 2
    assert skills[0]["skill"] == "good-skill"  # best utility first


def test_proposals_list_and_pending_filter(hermes_home):
    from hermes_cli.web_routers.skill_evolution import skill_evolution_proposals
    import asyncio

    _seed_proposal(hermes_home, "bad-skill")
    data = asyncio.run(skill_evolution_proposals())
    assert data["total"] >= 1
    assert data["proposals"][0]["skill"] == "bad-skill"

    pending = asyncio.run(skill_evolution_proposals(pending_only=True))
    assert pending["total"] >= 1


def test_approve_applies_bounded_edit(hermes_home):
    """Approve patches the target section of SKILL.md."""
    from hermes_cli.web_routers.skill_evolution import approve_proposal
    import asyncio

    skill_dir = _write_skill(hermes_home / "skills", "demo-skill")
    _seed_proposal(hermes_home, "demo-skill")

    # Find the proposal id
    from agent.skill_reflection import list_proposals
    prop = list_proposals("demo-skill")[0]

    result = asyncio.run(approve_proposal("demo-skill", prop["proposal_id"]))
    assert result["ok"] is True
    assert result["path"] == str(skill_dir / "SKILL.md")
    assert result["heading"] == "## Pitfalls"

    # Verify the fix was applied inside the Pitfalls section
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Add token-refresh note before calls" in content
    pitfalls_start = content.index("## Pitfalls")
    verification_start = content.index("## Verification")
    assert pitfalls_start < content.index("Add token-refresh") < verification_start

    # Proposal should be removed from queue (applied)
    from agent.skill_reflection import list_proposals
    assert len([p for p in list_proposals("demo-skill") if p["status"] == "pending"]) == 0


def test_approve_missing_section_appends(hermes_home):
    """When the target section is missing, approve appends a new section."""
    from hermes_cli.web_routers.skill_evolution import approve_proposal
    import asyncio

    skill_dir = _write_skill(hermes_home / "skills", "demo-skill")
    # Remove the Verification section from the skill
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    content = content.split("## Verification")[0]
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    _seed_proposal(hermes_home, "demo-skill")
    from agent.skill_reflection import list_proposals
    prop = [p for p in list_proposals("demo-skill") if p["target_section"] == "pitfalls"][0]

    result = asyncio.run(approve_proposal("demo-skill", prop["proposal_id"]))
    assert result["ok"] is True
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Add token-refresh note before calls" in content


def test_reject_keeps_for_audit(hermes_home):
    from hermes_cli.web_routers.skill_evolution import reject_proposal
    import asyncio

    _write_skill(hermes_home / "skills", "demo-skill")
    _seed_proposal(hermes_home, "demo-skill")
    from agent.skill_reflection import list_proposals
    prop = list_proposals("demo-skill")[0]

    result = asyncio.run(reject_proposal("demo-skill", prop["proposal_id"]))
    assert result["ok"] is True
    assert result["status"] == "rejected"
    # Skill content unchanged
    content = (hermes_home / "skills" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "Add token-refresh" not in content


def test_approve_missing_proposal_404(hermes_home):
    from hermes_cli.web_routers.skill_evolution import approve_proposal
    from fastapi import HTTPException
    import asyncio

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(approve_proposal("demo-skill", "nonexistent-id"))
    assert excinfo.value.status_code == 404


def test_approve_missing_skill_404(hermes_home):
    from hermes_cli.web_routers.skill_evolution import approve_proposal
    from fastapi import HTTPException
    import asyncio

    _seed_proposal(hermes_home, "ghost-skill")
    from agent.skill_reflection import list_proposals
    prop = list_proposals("ghost-skill")[0]

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(approve_proposal("ghost-skill", prop["proposal_id"]))
    assert excinfo.value.status_code == 404
