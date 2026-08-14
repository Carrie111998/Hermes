import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_breakdown import _SKILLS_BLOCK_RE
from agent.prompt_builder import (
    _build_skills_manifest,
    _skills_prompt_snapshot_path,
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
)
from tools.skills_tool import skills_list


@pytest.fixture(autouse=True)
def _clear_skills_cache():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _write_skill(root, category, name, description):
    skill_dir = root / "skills" / category / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n"
    )


def test_category_index_hides_unpinned_names_and_keeps_pinned_details(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "devops", "hermes-agent", "Operate Hermes safely")
    _write_skill(tmp_path, "devops", "rare-infra", "Rare infrastructure procedure")
    _write_skill(tmp_path, "research", "deep-research", "Research obscure topics")

    result = build_skills_system_prompt(
        index_mode="categories",
        pinned_skills=frozenset({"hermes-agent"}),
    )

    assert "devops (2 skills)" in result
    assert "research (1 skill)" in result
    assert "hermes-agent: Operate Hermes safely" in result
    assert "rare-infra" not in result
    assert "Rare infrastructure procedure" not in result
    assert "deep-research" not in result
    assert "skills_list(category" in result
    assert "skill_view(name" in result
    assert "<available_skills>" in result
    assert "</available_skills>" in result
    parsed_block = _SKILLS_BLOCK_RE.search(result)
    assert parsed_block is not None
    assert "<skill_categories>" in parsed_block.group(0)
    assert "<pinned_skills>" in parsed_block.group(0)


def test_category_index_cache_isolated_by_mode_and_pins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "tools", "skill-one", "Description for skill-one")
    _write_skill(tmp_path, "tools", "skill-two", "Description for skill-two")

    compact = build_skills_system_prompt(
        index_mode="categories",
        pinned_skills=frozenset({"skill-one"}),
    )
    assert "skill-one" in compact and "skill-two" not in compact

    other_pin = build_skills_system_prompt(
        index_mode="categories",
        pinned_skills=frozenset({"skill-two"}),
    )
    assert "skill-two" in other_pin and "skill-one" not in other_pin

    full = build_skills_system_prompt(index_mode="full")
    assert "skill-one" in full and "skill-two" in full


def test_invalid_index_mode_falls_back_to_full(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "tools", "visible-skill", "Must remain visible")

    result = build_skills_system_prompt(index_mode="typo")
    assert "visible-skill" in result
    assert "Must remain visible" in result


def test_rendered_nested_category_is_queryable_via_skills_list(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "mlops/models", "clip", "Vision-language models")

    result = build_skills_system_prompt(index_mode="categories")
    listing = json.loads(skills_list(category="mlops"))

    assert "mlops (1 skill)" in result
    assert "mlops/models" not in result
    assert [skill["name"] for skill in listing["skills"]] == ["clip"]


def test_category_mode_does_not_change_full_mode_nested_grouping(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "mlops/models", "clip", "Vision-language models")

    full = build_skills_system_prompt(index_mode="full")
    compact = build_skills_system_prompt(index_mode="categories")

    assert "  mlops/models:" in full
    assert "  mlops:" not in full
    assert "mlops (1 skill)" in compact


def test_rendered_general_category_is_queryable_via_skills_list(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "", "flat-skill", "A root-level skill")

    result = build_skills_system_prompt(index_mode="categories")
    listing = json.loads(skills_list(category="general"))

    assert "general (1 skill)" in result
    assert [skill["name"] for skill in listing["skills"]] == ["flat-skill"]


def test_discovery_category_schema_invalidates_version_two_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_skill(tmp_path, "mlops/models", "clip", "Vision-language models")
    skills_dir = tmp_path / "skills"
    stale_snapshot = {
        "version": 2,
        "manifest": _build_skills_manifest(skills_dir),
        "skills": [
            {
                "skill_name": "clip",
                "category": "mlops/models",
                "frontmatter_name": "clip",
                "description": "Vision-language models",
                "platforms": [],
                "conditions": {},
            }
        ],
        "category_descriptions": {},
    }
    _skills_prompt_snapshot_path().write_text(json.dumps(stale_snapshot))

    result = build_skills_system_prompt(index_mode="categories")

    assert "mlops (1 skill)" in result
    assert "mlops/models" not in result


def test_system_prompt_passes_category_mode_and_pins_from_config():
    from agent.system_prompt import build_system_prompt_parts

    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names={"skills_list", "skill_view"},
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides={},
        model="",
        provider="",
        platform="cli",
        pass_session_id=False,
        session_id="",
    )

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
        patch("run_agent.get_toolset_for_tool", return_value="skills"),
        patch("run_agent.build_skills_system_prompt", return_value="CATEGORY INDEX") as build,
        patch("hermes_cli.config.load_config", return_value={
            "skills": {
                "index_mode": "categories",
                "pinned": ["hermes-agent", "github-development-workflows"],
            }
        }),
        patch(
            "agent.coding_context.coding_compact_skill_categories",
            return_value=frozenset(),
        ),
        patch(
            "agent.coding_context.coding_system_prompt_parts",
            return_value=([], [], []),
        ),
    ):
        parts = build_system_prompt_parts(agent)

    assert "CATEGORY INDEX" in parts["volatile"]
    build.assert_called_once_with(
        available_tools=agent.valid_tool_names,
        available_toolsets={"skills"},
        compact_categories=None,
        index_mode="categories",
        pinned_skills=frozenset(
            {"hermes-agent", "github-development-workflows"}
        ),
    )
