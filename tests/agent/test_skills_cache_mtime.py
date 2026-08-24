"""Test that skills cache invalidates when a new skill is installed (#92313)."""
import sys
from pathlib import Path

import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache


def test_new_skill_visible_without_manual_cache_clear(tmp_path, monkeypatch):
    # Setup isolated HERMES_HOME
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    skills_dir = hermes_home / "skills"
    skills_dir.mkdir()
    
    # Create an initial skill
    skill_a = skills_dir / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("---\nname: skill-a\ndescription: Test skill A.\n---\n# Skill A\n", encoding="utf-8")
    
    clear_skills_system_prompt_cache()
    first = build_skills_system_prompt(skills_dir_override=skills_dir)
    assert "skill-a" in first
    
    # Install a new skill (simulating `hermes skills install`)
    skill_b = skills_dir / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("---\nname: skill-b\ndescription: Test skill B.\n---\n# Skill B\n", encoding="utf-8")
    
    # Without clearing cache, new skill should still be visible due to manifest fingerprint
    second = build_skills_system_prompt(skills_dir_override=skills_dir)
    assert "skill-b" in second, "Newly installed skill should be visible without manual cache clear"
    assert "skill-a" in second
