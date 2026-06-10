import pytest

from hermes_cli.ultra_studio_skills import (
    DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST,
    apply_ultra_studio_allowlist,
    build_disabled_skills_config,
    collect_skill_names,
    compute_disabled_skills,
)


def test_compute_disabled_skills_keeps_only_ultra_studio_allowlist():
    installed = [
        "workflow-router",
        "infographic-md-flow",
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
    }


def test_ultra_allowlist_hides_unrelated_skills_from_discovery(tmp_path, monkeypatch):
    installed = [
        "workflow-router",
        "infographic-md-flow",
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
