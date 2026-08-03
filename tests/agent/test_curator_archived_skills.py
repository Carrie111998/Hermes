"""Regression coverage for archived curator consolidation candidates."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def curator_archived_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    import tools.skill_usage as skill_usage
    import agent.curator as curator

    importlib.reload(skill_usage)
    importlib.reload(curator)
    monkeypatch.setattr(skill_usage, "_prune_builtins_enabled", lambda: False)
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    return {"home": home, "skills": skills, "usage": skill_usage, "curator": curator}


def _write_skill(skills: Path, name: str) -> Path:
    skill_dir = skills / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _archive_managed_skill(env, name: str, *, absorbed_into: str | None = None) -> None:
    usage = env["usage"]
    _write_skill(env["skills"], name)
    usage.mark_agent_created(name)
    ok, message = usage.archive_skill(name, absorbed_into=absorbed_into)
    assert ok, message


def test_archived_skill_is_a_consolidation_candidate(curator_archived_env, monkeypatch):
    env = curator_archived_env
    usage = env["usage"]
    curator = env["curator"]
    _archive_managed_skill(env, "historical-api")
    usage.bump_use("historical-api")

    monkeypatch.setattr(curator, "_load_config", lambda: {"consolidate_archived": True})
    rows = usage.curated_report(include_archived=True)
    listing = curator._render_candidate_list()

    row = next(row for row in rows if row["name"] == "historical-api")
    assert row["state"] == usage.STATE_ARCHIVED
    assert row["activity_count"] == 1
    assert "- historical-api" in listing
    assert "state=archived" in listing
    assert "archive_path=" in listing
    assert "historical-api" in listing


def test_absorbed_skill_is_filtered_from_future_passes(curator_archived_env, monkeypatch):
    env = curator_archived_env
    usage = env["usage"]
    curator = env["curator"]
    _write_skill(env["skills"], "already-merged")
    usage.mark_agent_created("already-merged")
    assert usage.mark_absorbed("already-merged", "general-umbrella") is True

    monkeypatch.setattr(curator, "_load_config", lambda: {"consolidate_archived": True})
    assert "already-merged" not in usage.list_agent_created_skill_names()
    assert "already-merged" not in curator._render_candidate_list()


def test_absorbed_marker_survives_archive_restore_until_adopt(curator_archived_env):
    env = curator_archived_env
    usage = env["usage"]
    curator = env["curator"]
    name = "restored-merged"
    _write_skill(env["skills"], name)
    usage.mark_agent_created(name)

    ok, message = usage.archive_skill(name, absorbed_into="general-umbrella")
    assert ok, message
    assert usage.get_record(name)["absorbed_into"] == "general-umbrella"

    ok, message = usage.restore_skill(name)
    assert ok, message
    restored = usage.get_record(name)
    assert restored["state"] == usage.STATE_ACTIVE
    assert restored["absorbed_into"] == "general-umbrella"
    assert name not in curator._render_candidate_list(include_archived=True)

    ok, message = usage.adopt_skill(name)
    assert ok, message
    assert usage.get_record(name).get("absorbed_into") is None
    assert name in curator._render_candidate_list(include_archived=True)


def test_archived_candidates_are_config_gated(curator_archived_env, monkeypatch):
    env = curator_archived_env
    curator = env["curator"]
    _archive_managed_skill(env, "gated-history")

    monkeypatch.setattr(curator, "_load_config", lambda: {})
    assert "gated-history" not in curator._render_candidate_list()

    monkeypatch.setattr(curator, "_load_config", lambda: {"consolidate_archived": True})
    assert "gated-history" in curator._render_candidate_list()
