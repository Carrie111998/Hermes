import argparse

import pytest

from hermes_cli.ultra_studio_skills import (
    DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST,
    DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST,
    VIDEO_AGENT_CORE_SKILL_ALLOWLIST,
    VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST,
    VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST,
    apply_ultra_studio_allowlist,
    build_disabled_skills_config,
    collect_skill_names,
    compute_disabled_skills,
)


def test_compute_disabled_skills_keeps_only_ultra_studio_allowlist():
    installed = [
        "workflow-router",
        "infographic-md-flow",
        "gpt-image-2-director",
        "marketing-studio-director",
        "higgsfield-content-factory",
        "ascii-video",
        "comfyui",
        "media-qa",
        "prompt-repair",
        "youtube-content",
    ]

    assert compute_disabled_skills(installed) == [
        "ascii-video",
        "comfyui",
        "youtube-content",
    ]


def test_compute_disabled_skills_accepts_discovery_rows():
    installed = [
        {"name": "workflow-router", "category": "creative"},
        {"name": "github-auth", "category": "github"},
        {"name": "media-qa", "category": "creative"},
    ]

    assert compute_disabled_skills(installed) == ["github-auth"]


def test_collect_skill_names_deduplicates_and_sorts():
    assert collect_skill_names(["z", {"name": "a"}, "z"]) == ["a", "z"]


def test_collect_skill_names_rejects_bad_rows():
    with pytest.raises(ValueError):
        collect_skill_names([{"title": "missing-name"}])


def test_build_disabled_skills_config_global():
    config = build_disabled_skills_config(["workflow-router", "linear"])

    assert config == {"skills": {"disabled": ["linear"]}}


def test_build_disabled_skills_config_platform():
    config = build_disabled_skills_config(
        ["workflow-router", "linear"],
        platform="cli",
    )

    assert config == {"skills": {"platform_disabled": {"cli": ["linear"]}}}


def test_apply_ultra_studio_allowlist_is_side_effect_free():
    original = {"skills": {"disabled": ["old"], "platform_disabled": {"cli": ["old"]}}}

    updated = apply_ultra_studio_allowlist(
        original,
        ["workflow-router", "spotify"],
        platform="cli",
    )

    assert original["skills"]["platform_disabled"]["cli"] == ["old"]
    assert updated["skills"]["disabled"] == ["old"]
    assert updated["skills"]["platform_disabled"]["cli"] == ["spotify"]


def test_default_allowlist_names_are_present():
    assert set(DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST) == {
        "workflow-router",
        "infographic-md-flow",
        "media-qa",
        "prompt-repair",
        "gpt-image-2-director",
        "marketing-studio-director",
        "higgsfield-content-factory",
    }
    assert DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST == DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST


def test_video_agent_allowlist_separates_core_from_specific_workflows():
    assert set(VIDEO_AGENT_CORE_SKILL_ALLOWLIST) == {
        "workflow-router",
        "media-qa",
        "prompt-repair",
    }
    assert VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST == ("infographic-md-flow",)
    assert VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST == (
        "gpt-image-2-director",
        "marketing-studio-director",
        "higgsfield-content-factory",
    )


def test_ultra_allowlist_hides_unrelated_skills_from_discovery(tmp_path, monkeypatch):
    installed = [
        "workflow-router",
        "infographic-md-flow",
        "gpt-image-2-director",
        "marketing-studio-director",
        "higgsfield-content-factory",
        "media-qa",
        "prompt-repair",
        "ascii-video",
        "github-auth",
    ]
    for name in installed:
        skill_dir = tmp_path / "creative" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} test skill\n---\nBody",
            encoding="utf-8",
        )

    import agent.skill_utils as _su
    import tools.skills_tool as _st

    disabled = set(compute_disabled_skills(installed))
    monkeypatch.setattr(_st, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(_st, "_get_disabled_skill_names", lambda: disabled)
    monkeypatch.setattr(_st, "skill_matches_platform", lambda frontmatter: True)
    monkeypatch.setattr(_su, "get_external_skills_dirs", lambda: [])

    visible = {skill["name"] for skill in _st._find_all_skills()}

    assert visible == set(DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST)


def test_apply_skill_allowlist_disables_everything_outside_allowlist(monkeypatch):
    import hermes_cli.skills_config as skills_config

    saved = {}

    monkeypatch.setattr(skills_config, "load_config", lambda: {"skills": {}})
    monkeypatch.setattr(
        skills_config,
        "_list_all_skills",
        lambda **kwargs: [
            {"name": "workflow-router"},
            {"name": "prompt-repair"},
            {"name": "ascii-video"},
        ],
    )

    def fake_save_disabled_skills(config, disabled, platform=None):
        saved["config"] = config
        saved["disabled"] = disabled
        saved["platform"] = platform

    monkeypatch.setattr(
        skills_config,
        "save_disabled_skills",
        fake_save_disabled_skills,
    )

    result = skills_config.apply_skill_allowlist(
        ["workflow-router", "prompt-repair"],
        platform="cli",
    )

    assert saved["disabled"] == {"ascii-video"}
    assert saved["platform"] == "cli"
    assert result["enabled"] == ["prompt-repair", "workflow-router"]
    assert result["disabled"] == ["ascii-video"]


def test_apply_skill_allowlist_fails_closed_when_discovery_errors(monkeypatch):
    import hermes_cli.skills_config as skills_config

    monkeypatch.setattr(skills_config, "load_config", lambda: {"skills": {}})

    def fail_discovery(*args, **kwargs):
        raise skills_config.SkillDiscoveryError("scan failed")

    def fail_if_saved(*args, **kwargs):
        raise AssertionError("config must not be saved when discovery fails")

    monkeypatch.setattr(skills_config, "_list_all_skills", fail_discovery)
    monkeypatch.setattr(skills_config, "save_disabled_skills", fail_if_saved)

    with pytest.raises(skills_config.SkillDiscoveryError):
        skills_config.apply_skill_allowlist(["workflow-router"])


def test_video_agent_skills_command_uses_core_allowlist_and_global_platform(monkeypatch):
    import hermes_cli.skills_config as skills_config

    captured = {}

    def fake_apply(allowlist, *, platform=None):
        captured["allowlist"] = tuple(allowlist)
        captured["platform"] = platform
        return {
            "disabled": ["ascii-video"],
            "enabled": ["workflow-router"],
            "missing": [],
            "platform": platform,
        }

    monkeypatch.setattr(skills_config, "apply_skill_allowlist", fake_apply)

    args = argparse.Namespace(core_only=True, platform="global")
    skills_config.video_agent_skills_command(args)

    assert captured["allowlist"] == VIDEO_AGENT_CORE_SKILL_ALLOWLIST
    assert captured["platform"] is None


def test_video_agent_skills_parser_rejects_unknown_platform():
    from hermes_cli.subcommands.skills import build_skills_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_skills_parser(subparsers, cmd_skills=lambda args: None)

    parsed = parser.parse_args(["skills", "video-agent", "--platform", "api_server"])
    assert parsed.platform == "api_server"

    with pytest.raises(SystemExit):
        parser.parse_args(["skills", "video-agent", "--platform", "typo"])
