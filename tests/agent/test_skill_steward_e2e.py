"""End-to-end contract for route -> load -> outcome -> curator."""

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _write_skill(skills_dir: Path, name: str, description: str) -> None:
    root = skills_dir / "testing" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo it safely.\n",
        encoding="utf-8",
    )


def test_route_load_outcome_then_curator_preserves_never_tried_skill(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import tools.skill_usage as skill_usage
    import tools.skills_tool as skills_tool
    import agent.curator as curator

    importlib.reload(skill_usage)
    importlib.reload(skills_tool)
    importlib.reload(curator)
    skills_tool._SKILLS_CACHE.clear()

    _write_skill(skills_dir, "deploy-helper", "Deploy Kubernetes workloads safely.")
    _write_skill(skills_dir, "never-tried", "Handle a rare unrelated workflow.")

    now = datetime(2026, 4, 30, tzinfo=timezone.utc)
    old = (now - timedelta(days=180)).isoformat()
    skill_usage.save_usage({
        "deploy-helper": {
            "created_by": "agent",
            "created_at": old,
            "state": "active",
        },
        "never-tried": {
            "created_by": "agent",
            "created_at": old,
            "state": "active",
        },
    })

    ranked = json.loads(skills_tool.skills_list(query="deploy kubernetes", limit=1))
    assert ranked["skills"][0]["name"] == "deploy-helper"

    loaded = json.loads(skills_tool.skill_view("deploy-helper"))
    assert loaded["success"] is True
    skill_usage.bump_use("deploy-helper", task_id="task-1", session_id="session-1")
    skill_usage.record_outcome("deploy-helper", "success", task_id="task-1")
    usage = skill_usage.load_usage()
    usage["deploy-helper"]["last_used_at"] = old
    usage["deploy-helper"]["last_outcome_at"] = old
    skill_usage.save_usage(usage)

    monkeypatch.setattr(curator, "get_stale_after_days", lambda: 30)
    monkeypatch.setattr(curator, "get_archive_after_days", lambda: 90)
    counts = curator.apply_automatic_transitions(now=now)

    assert counts["archived"] == 1
    assert not (skills_dir / "testing" / "deploy-helper").exists()
    assert (skills_dir / "testing" / "never-tried" / "SKILL.md").exists()
    assert skill_usage.get_record("never-tried")["state"] == "stale"

    usage_text = (skills_dir / ".usage.json").read_text(encoding="utf-8")
    assert "deploy kubernetes" not in usage_text
    assert "Do it safely" not in usage_text
