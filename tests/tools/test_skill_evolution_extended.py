"""Tests for the extended skill-evolution API: detail drawer, one-click
proposal generation, manual outcome recording, proposal status filtering."""

import asyncio
import os
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
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


def _seed_failures(home, skill, n=6, error_type="api_403"):
    from tools.skill_usage import record_outcome
    for _ in range(n):
        record_outcome(skill, "failure", error_type=error_type)
    for _ in range(4):
        record_outcome(skill, "success")


def test_detail_drawer(hermes_home):
    from hermes_cli.web_routers.skill_evolution import skill_evolution_detail

    _seed_failures(hermes_home, "demo")
    d = asyncio.run(skill_evolution_detail("demo"))
    assert d["skill"] == "demo"
    assert d["summary"]["failure_count"] == 6
    assert d["summary"]["success_count"] == 4
    assert d["utility_score"] is not None
    assert any("api_403" in str(t) for t in d["failure_patterns"].get("top_error_types", []))
    assert isinstance(d["proposals"], list)


def test_propose_generates(hermes_home):
    from hermes_cli.web_routers.skill_evolution import propose_skill_improvement

    _seed_failures(hermes_home, "demo")
    r = asyncio.run(propose_skill_improvement("demo"))
    assert r["ok"] is True
    assert r["proposal"]["skill"] == "demo"
    assert r["proposal"]["status"] == "pending"
    assert "api_403" in str(r["proposal"].get("failure_types") or [])


def test_propose_already_pending(hermes_home):
    from hermes_cli.web_routers.skill_evolution import propose_skill_improvement

    _seed_failures(hermes_home, "demo")
    asyncio.run(propose_skill_improvement("demo"))
    r2 = asyncio.run(propose_skill_improvement("demo"))
    assert r2["ok"] is False
    assert r2["reason"] == "already_pending"


def test_propose_no_signals_400(hermes_home):
    from hermes_cli.web_routers.skill_evolution import propose_skill_improvement
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        asyncio.run(propose_skill_improvement("clean-skill"))
    assert e.value.status_code == 400


def test_manual_outcome(hermes_home):
    from hermes_cli.web_routers.skill_evolution import record_skill_outcome_manual

    r = asyncio.run(record_skill_outcome_manual("demo", {"outcome": "success"}))
    assert r["ok"] is True
    assert r["outcome"] == "success"

    r2 = asyncio.run(record_skill_outcome_manual("demo", {"outcome": "failure", "error_type": "timeout"}))
    assert r2["ok"] is True
    assert r2["outcome"] == "failure"


def test_manual_outcome_invalid(hermes_home):
    from hermes_cli.web_routers.skill_evolution import record_skill_outcome_manual
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        asyncio.run(record_skill_outcome_manual("demo", {"outcome": "banana"}))
    assert e.value.status_code == 400


def test_proposals_status_filter(hermes_home):
    from hermes_cli.web_routers.skill_evolution import (
        approve_proposal,
        skill_evolution_proposals,
    )

    # Seed a skill + failure signals + proposal, then approve it
    from pathlib import Path
    skill_dir = hermes_home / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\n---\n# Demo\n## Pitfalls\n- x\n## Verification\n- y\n",
        encoding="utf-8",
    )
    _seed_failures(hermes_home, "demo")

    from hermes_cli.web_routers.skill_evolution import propose_skill_improvement
    r = asyncio.run(propose_skill_improvement("demo"))
    pid = r["proposal"]["proposal_id"]
    asyncio.run(approve_proposal("demo", pid))

    # Applied proposals are now retained for the history view (marked applied,
    # not deleted), so the applied filter must include it and pending must not.
    applied = asyncio.run(skill_evolution_proposals(status="applied"))
    assert any(p["proposal_id"] == pid for p in applied["proposals"])
    pending = asyncio.run(skill_evolution_proposals(status="pending"))
    assert all(p["proposal_id"] != pid for p in pending["proposals"])


def test_route_registration():
    from hermes_cli.web_routers import skill_evolution

    paths = {r.path for r in skill_evolution.router.routes}
    assert "/api/skills/evolution/skills/{skill}" in paths
    assert "/api/skills/evolution/skills/{skill}/propose" in paths
    assert "/api/skills/evolution/skills/{skill}/outcome" in paths
