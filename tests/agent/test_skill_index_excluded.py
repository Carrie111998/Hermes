"""End-to-end coverage for hidden-but-loadable skill catalog entries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest


def _write_skill(
    root: Path,
    name: str,
    body: str | None = None,
    *,
    frontmatter_name: str | None = None,
) -> Path:
    skill_dir = root / "general" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    declared_name = frontmatter_name or name
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared_name}\ndescription: Description for {declared_name}.\n---\n\n"
        f"# {name}\n\n{body or f'Body for {name}.'}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    skills = home / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    from agent import prompt_builder, skill_commands, skill_utils
    from gateway import session_context
    from tools import skills_tool

    for var in session_context._VAR_MAP.values():
        var.set(session_context._UNSET)
    prompt_builder.drain_truncation_warnings()
    skill_utils._external_dirs_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    skills_tool._SKILLS_CACHE.clear()
    skill_commands._skill_commands = {}
    skill_commands._skill_commands_platform = None
    yield home, skills
    for var in session_context._VAR_MAP.values():
        var.set(session_context._UNSET)
    prompt_builder.drain_truncation_warnings()
    skill_utils._external_dirs_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    skills_tool._SKILLS_CACHE.clear()
    skill_commands._skill_commands = {}
    skill_commands._skill_commands_platform = None


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")
    from agent import prompt_builder, skill_utils
    from tools import skills_tool

    skill_utils._external_dirs_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    skills_tool._SKILLS_CACHE.clear()


def test_index_excluded_skill_is_hidden_from_prompt_list_and_slash_discovery(
    isolated_profile,
):
    home, skills = isolated_profile
    _write_skill(skills, "visible-skill")
    _write_skill(skills, "hidden-skill")
    _write_config(home, "skills:\n  index_excluded: [hidden-skill]\n")

    from agent.prompt_builder import build_skills_system_prompt
    from agent.skill_commands import scan_skill_commands
    from tools.skills_tool import skills_list

    prompt = build_skills_system_prompt()
    listed = json.loads(skills_list())
    with patch("tools.skills_tool.SKILLS_DIR", skills):
        commands = scan_skill_commands()

    assert "visible-skill" in prompt
    assert "hidden-skill" not in prompt
    assert {item["name"] for item in listed["skills"]} == {"visible-skill"}
    assert "/visible-skill" in commands
    assert "/hidden-skill" not in commands


def test_index_excluded_skill_remains_exactly_viewable_and_force_loadable(
    isolated_profile,
):
    home, skills = isolated_profile
    _write_skill(skills, "hidden-skill", "HIDDEN BUT LOADABLE")
    _write_config(home, "skills:\n  index_excluded: [hidden-skill]\n")

    from agent.skill_commands import build_preloaded_skills_prompt
    from tools.skills_tool import skill_view

    viewed = json.loads(skill_view("hidden-skill", preprocess=False))
    preloaded, loaded, missing = build_preloaded_skills_prompt(["hidden-skill"])

    assert viewed["success"] is True
    assert "HIDDEN BUT LOADABLE" in viewed["content"]
    assert loaded == ["hidden-skill"]
    assert missing == []
    assert "HIDDEN BUT LOADABLE" in preloaded


def test_disabled_wins_over_index_excluded_for_view_and_force_load(
    isolated_profile,
):
    home, skills = isolated_profile
    _write_skill(skills, "blocked-skill", "MUST NOT LOAD")
    _write_config(
        home,
        "skills:\n"
        "  disabled: [blocked-skill]\n"
        "  index_excluded: [blocked-skill]\n",
    )

    from agent.skill_commands import build_preloaded_skills_prompt
    from tools.skills_tool import skill_view

    viewed = json.loads(skill_view("blocked-skill", preprocess=False))
    preloaded, loaded, missing = build_preloaded_skills_prompt(["blocked-skill"])

    assert viewed["success"] is False
    assert "disabled" in viewed["error"].lower()
    assert loaded == []
    assert missing == ["blocked-skill"]
    assert "MUST NOT LOAD" not in preloaded


def test_index_excluded_applies_to_external_dirs_without_blocking_exact_view(
    isolated_profile,
):
    home, skills = isolated_profile
    external = home.parent / "external-skills"
    _write_skill(external, "external-hidden", "EXTERNAL LOADABLE")
    _write_config(
        home,
        f"skills:\n  external_dirs: [{external}]\n  index_excluded: [external-hidden]\n",
    )

    from agent.prompt_builder import build_skills_system_prompt
    from tools.skills_tool import skill_view, skills_list

    prompt = build_skills_system_prompt()
    listed = json.loads(skills_list())
    viewed = json.loads(skill_view("external-hidden", preprocess=False))

    assert "external-hidden" not in prompt
    assert "external-hidden" not in {item["name"] for item in listed["skills"]}
    assert viewed["success"] is True
    assert "EXTERNAL LOADABLE" in viewed["content"]


def test_directory_alias_exclusion_hides_every_discovery_surface(isolated_profile):
    home, skills = isolated_profile
    _write_skill(skills, "directory-alias", frontmatter_name="canonical-name")
    _write_config(home, "skills:\n  index_excluded: [directory-alias]\n")

    from agent.prompt_builder import build_skills_system_prompt
    from agent.skill_commands import scan_skill_commands
    from tools.skills_tool import skill_view, skills_list

    prompt = build_skills_system_prompt()
    listed = {item["name"] for item in json.loads(skills_list())["skills"]}
    commands = scan_skill_commands()
    viewed = json.loads(skill_view("canonical-name", preprocess=False))

    assert "canonical-name" not in prompt
    assert "canonical-name" not in listed
    assert "/canonical-name" not in commands
    assert viewed["success"] is True


def test_platform_index_excluded_is_additive_and_platform_scoped(isolated_profile, monkeypatch):
    home, skills = isolated_profile
    _write_skill(skills, "global-hidden")
    _write_skill(skills, "telegram-hidden")
    _write_skill(skills, "visible-skill")
    _write_config(
        home,
        "skills:\n"
        "  index_excluded: [global-hidden]\n"
        "  platform_index_excluded:\n"
        "    telegram: [telegram-hidden]\n",
    )

    from agent import skill_utils
    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache

    assert skill_utils.get_index_excluded_skill_names("telegram") == {
        "global-hidden",
        "telegram-hidden",
    }
    assert skill_utils.get_index_excluded_skill_names("discord") == {"global-hidden"}

    monkeypatch.setenv("HERMES_PLATFORM", "telegram")
    clear_skills_system_prompt_cache(clear_snapshot=True)
    telegram_prompt = build_skills_system_prompt()
    monkeypatch.setenv("HERMES_PLATFORM", "discord")
    clear_skills_system_prompt_cache(clear_snapshot=True)
    discord_prompt = build_skills_system_prompt()

    assert "global-hidden" not in telegram_prompt
    assert "telegram-hidden" not in telegram_prompt
    assert "visible-skill" in telegram_prompt
    assert "global-hidden" not in discord_prompt
    assert "telegram-hidden" in discord_prompt


def test_hermes_platform_drives_list_slash_and_disabled_precedence(
    isolated_profile, monkeypatch
):
    home, skills = isolated_profile
    _write_skill(skills, "tg-hidden")
    _write_skill(skills, "tg-disabled")
    _write_skill(skills, "visible-skill")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    telegram: [tg-hidden]\n"
        "  platform_disabled:\n"
        "    telegram: [tg-disabled]\n",
    )
    monkeypatch.setenv("HERMES_PLATFORM", "telegram")

    from agent.skill_commands import scan_skill_commands
    from tools.skills_tool import skill_view, skills_list

    listed = {item["name"] for item in json.loads(skills_list())["skills"]}
    commands = scan_skill_commands()

    assert listed == {"visible-skill"}
    assert set(commands) == {"/visible-skill"}
    assert json.loads(skill_view("tg-disabled", preprocess=False))["success"] is False
    assert json.loads(skill_view("tg-hidden", preprocess=False))["success"] is True


def test_cli_platform_exclusion_reaches_prompt_and_tool_discovery_without_env_override(
    isolated_profile, monkeypatch
):
    home, skills = isolated_profile
    _write_skill(skills, "cli-hidden")
    _write_skill(skills, "visible-skill")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    cli: [cli-hidden]\n",
    )
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")

    from agent.prompt_builder import build_skills_system_prompt
    from tools.skills_tool import skills_list

    prompt = build_skills_system_prompt(platform="desktop")
    listed = json.loads(skills_list())

    assert "cli-hidden" not in prompt
    assert "cli-hidden" not in {item["name"] for item in listed["skills"]}
    assert "visible-skill" in prompt


def test_hermes_platform_override_wins_over_agent_surface_for_prompt_index(
    isolated_profile, monkeypatch
):
    home, skills = isolated_profile
    _write_skill(skills, "telegram-hidden")
    _write_skill(skills, "visible-skill")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    telegram: [telegram-hidden]\n",
    )
    monkeypatch.setenv("HERMES_PLATFORM", "telegram")

    from agent.prompt_builder import build_skills_system_prompt

    prompt = build_skills_system_prompt(platform="desktop")

    assert "telegram-hidden" not in prompt
    assert "visible-skill" in prompt


def test_cleared_session_context_masks_stale_session_platform_env(
    isolated_profile, monkeypatch
):
    home, skills = isolated_profile
    _write_skill(skills, "cli-hidden")
    _write_skill(skills, "telegram-hidden")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    cli: [cli-hidden]\n"
        "    telegram: [telegram-hidden]\n",
    )
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")

    from agent.prompt_builder import build_skills_system_prompt
    from gateway.session_context import clear_session_vars

    clear_session_vars([])
    prompt = build_skills_system_prompt(platform="desktop")

    assert "cli-hidden" not in prompt
    assert "telegram-hidden" in prompt


def test_profile_scoped_hermes_home_controls_index_exclusion(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "thin"
    _write_skill(default_home / "skills", "shared-skill")
    _write_skill(profile_home / "skills", "shared-skill")
    (default_home / "config.yaml").write_text("skills: {}\n", encoding="utf-8")
    (profile_home / "config.yaml").write_text(
        "skills:\n  index_excluded: [shared-skill]\n", encoding="utf-8"
    )

    from agent import prompt_builder, skill_utils

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    skill_utils._external_dirs_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "shared-skill" in prompt_builder.build_skills_system_prompt()

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    skill_utils._external_dirs_cache_clear()
    prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
    assert "shared-skill" not in prompt_builder.build_skills_system_prompt()


def test_default_config_keeps_all_skills_discoverable(isolated_profile):
    home, skills = isolated_profile
    _write_skill(skills, "legacy-skill")
    _write_config(home, "skills: {}\n")

    from agent.prompt_builder import build_skills_system_prompt
    from tools.skills_tool import skills_list

    assert "legacy-skill" in build_skills_system_prompt()
    assert "legacy-skill" in {
        item["name"] for item in json.loads(skills_list())["skills"]
    }


def test_prompt_size_platform_counts_match_rendered_skill_index(isolated_profile):
    home, skills = isolated_profile
    _write_skill(skills, "cli-hidden")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    cli: [cli-hidden]\n",
    )

    from hermes_cli import prompt_size

    data = prompt_size.compute_prompt_breakdown("cli")

    assert data["skills"]["index_excluded"] == 1
    assert "cli-hidden" not in {
        item["name"] for item in data["skills_breakdown"]
    }


def test_prompt_size_counts_follow_runtime_platform_override(
    isolated_profile, monkeypatch
):
    home, skills = isolated_profile
    _write_skill(skills, "cli-hidden")
    _write_skill(skills, "cli-hidden-two")
    _write_skill(skills, "telegram-hidden")
    _write_config(
        home,
        "skills:\n"
        "  platform_index_excluded:\n"
        "    cli: [cli-hidden, cli-hidden-two]\n"
        "    telegram: [telegram-hidden]\n",
    )
    monkeypatch.setenv("HERMES_PLATFORM", "telegram")

    from hermes_cli import prompt_size

    data = prompt_size.compute_prompt_breakdown("cli")
    rendered_names = {item["name"] for item in data["skills_breakdown"]}

    assert data["skills"]["index_excluded"] == 1
    assert "telegram-hidden" not in rendered_names
    assert "cli-hidden" in rendered_names
    assert "cli-hidden-two" in rendered_names


def test_prompt_size_reports_active_index_excluded_and_disabled_counts(isolated_profile):
    home, skills = isolated_profile
    _write_skill(skills, "active-skill")
    _write_skill(skills, "hidden-skill")
    _write_skill(skills, "disabled-skill")
    _write_config(
        home,
        "skills:\n"
        "  index_excluded: [hidden-skill]\n"
        "  disabled: [disabled-skill]\n",
    )

    from hermes_cli import prompt_size

    data = prompt_size.compute_prompt_breakdown("cli")
    rendered = prompt_size.render_breakdown(data)

    assert data["skills"]["active"] == 1
    assert data["skills"]["index_excluded"] == 1
    assert data["skills"]["disabled"] == 1
    assert "1 active" in rendered
    assert "1 index-excluded" in rendered
    assert "1 disabled" in rendered
    assert "hidden-skill" not in {
        item["name"] for item in data["skills_breakdown"]
    }


def test_real_cli_preload_accepts_index_excluded_and_rejects_disabled(
    isolated_profile,
):
    home, skills = isolated_profile
    _write_skill(skills, "hidden-skill", "HIDDEN CLI LOADABLE")
    _write_skill(skills, "disabled-skill", "DISABLED CLI BLOCKED")
    _write_config(
        home,
        "skills:\n"
        "  index_excluded: [hidden-skill]\n"
        "  disabled: [disabled-skill]\n",
    )
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    hidden = subprocess.run(
        [sys.executable, str(repo / "cli.py"), "--skills", "hidden-skill", "--list-tools"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    disabled = subprocess.run(
        [sys.executable, str(repo / "cli.py"), "--skills", "disabled-skill", "--list-tools"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert hidden.returncode == 0, hidden.stderr
    assert disabled.returncode != 0
    assert "Unknown skill(s): disabled-skill" in disabled.stdout + disabled.stderr
