"""Regression coverage for the MCP-authority skills boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import skill_utils
from tools import skills_tool
from tools.skills_tool import (
    _find_all_skills,
    build_installed_skill_inventory,
    skill_view,
    skills_list,
)


PRIVATE_ID = "canonical-private-workflow-8675309"


def _write_skill(root: Path, directory: str, name: str, body: str = "Use this skill.") -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Description for {name}.\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def authority_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_utils._external_dirs_cache_clear()
    skills_tool._SKILLS_CACHE.clear()
    yield home
    skill_utils._external_dirs_cache_clear()
    skills_tool._SKILLS_CACHE.clear()


def _write_config(home: Path, *, external_dirs=(), authority_roots=(), extra: str = "") -> None:
    lines = ["skills:"]
    if external_dirs:
        lines.append("  external_dirs:")
        lines.extend(f"    - {path}" for path in external_dirs)
    if authority_roots:
        lines.append("  central_private_roots:")
        lines.extend(f"    - {path}" for path in authority_roots)
    if extra:
        lines.extend(extra.splitlines())
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _names(raw: str) -> set[str]:
    return {item["name"] for item in json.loads(raw)["skills"]}


def test_external_dir_equal_to_authority_root_exposes_no_canonical_records(authority_env, tmp_path):
    authority = tmp_path / "authority"
    authority.mkdir()
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(authority_env / "skills", "mcp-adapter", "mcp-adapter")
    _write_config(authority_env, external_dirs=[authority], authority_roots=[authority])

    listed = skills_list()
    inventory = build_installed_skill_inventory()
    route = skills_list(query=PRIVATE_ID)
    denied = skill_view(PRIVATE_ID)

    assert _names(listed) == {"mcp-adapter"}
    assert [record["name"] for record in inventory] == ["mcp-adapter"]
    assert json.loads(route)["route"] == []
    assert json.loads(denied)["success"] is False


def test_authority_subtree_is_pruned_but_external_siblings_remain_visible(authority_env, tmp_path):
    external = tmp_path / "external"
    authority = external / "private"
    _write_skill(external, "public", "public-skill")
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    assert _names(skills_list()) == {"public-skill"}
    assert json.loads(skill_view("public-skill"))["success"] is True
    assert json.loads(skill_view(PRIVATE_ID))["success"] is False
    assert json.loads(skill_view("private/canonical")) == {
        "success": False,
        "error": "This skill is available only through its configured MCP authority.",
    }


def test_external_dir_nested_inside_authority_root_is_excluded(authority_env, tmp_path):
    authority = tmp_path / "authority"
    external = authority / "published"
    _write_skill(external, "canonical", PRIVATE_ID)
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    assert json.loads(skills_list())["skills"] == []
    assert _find_all_skills(include_topology=True) == []


@pytest.mark.parametrize("alias_kind", ["external_alias", "nested_alias"])
def test_symlink_aliases_cannot_bypass_authority_exclusion(authority_env, tmp_path, alias_kind):
    authority = tmp_path / "authority"
    _write_skill(authority, "canonical", PRIVATE_ID)
    external = tmp_path / "external"
    external.mkdir()
    if alias_kind == "external_alias":
        external.rmdir()
        external.symlink_to(authority, target_is_directory=True)
    else:
        (external / "alias").symlink_to(authority, target_is_directory=True)
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    assert json.loads(skills_list())["skills"] == []
    denied = json.loads(skill_view(PRIVATE_ID))
    assert denied == {
        "success": False,
        "error": "This skill is available only through its configured MCP authority.",
    }


def test_authority_boundary_cache_tracks_profile_switch_and_config_mtime(authority_env, tmp_path, monkeypatch):
    external = tmp_path / "external"
    first_root = external / "first"
    second_root = external / "second"
    _write_skill(first_root, "private", "first-private")
    _write_skill(second_root, "private", "second-private")
    _write_skill(external, "public", "public-skill")
    _write_config(authority_env, external_dirs=[external], authority_roots=[])

    assert _names(skills_list()) == {"public-skill", "first-private", "second-private"}

    config = authority_env / "config.yaml"
    _write_config(authority_env, external_dirs=[external], authority_roots=[first_root])
    stat = config.stat()
    os.utime(config, (stat.st_atime + 10, stat.st_mtime + 10))
    assert _names(skills_list()) == {"public-skill", "second-private"}

    other_home = tmp_path / "other-home"
    other_home.mkdir()
    _write_skill(other_home / "skills", "adapter", "other-profile-adapter")
    _write_config(other_home, authority_roots=[])
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    assert _names(skills_list()) == {"other-profile-adapter"}


def test_native_artifacts_do_not_leak_private_inventory_details(authority_env, tmp_path):
    external = tmp_path / "external"
    authority = external / "private"
    _write_skill(authority, "canonical", PRIVATE_ID, body="x" * 431)
    _write_skill(authority_env / "skills", "mcp-adapter", "mcp-adapter")
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    assert skill_utils.is_excluded_skill_path(authority / "canonical" / "SKILL.md")

    from hermes_cli import skills_topology

    outputs = [
        skills_list(),
        skills_list(query=PRIVATE_ID),
        skill_view(PRIVATE_ID),
    ]
    with patch("builtins.print") as printed:
        skills_topology.skills_topology_command(
            type("Args", (), {"skills_action": "topology", "json": True})()
        )
    outputs.extend(str(call.args[0]) for call in printed.call_args_list)

    for output in outputs:
        assert PRIVATE_ID not in output
        assert str(authority) not in output
        assert "query_digest" not in output
        assert "431" not in output


def test_native_prompt_and_slash_scans_exclude_mcp_authority_roots(authority_env, tmp_path):
    external = tmp_path / "external"
    authority = external / "private"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(external, "public", "public-skill")
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
    from agent.skill_commands import scan_skill_commands

    clear_skills_system_prompt_cache(clear_snapshot=True)
    prompt = build_skills_system_prompt()
    commands = scan_skill_commands()

    assert PRIVATE_ID not in prompt
    assert "public-skill" in prompt
    assert f"/{PRIVATE_ID}" not in commands
    assert "/public-skill" in commands


def test_native_management_and_gateway_scans_do_not_index_authority_skills(authority_env, tmp_path):
    external = tmp_path / "external"
    authority = external / "private"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    from gateway.run import _check_unavailable_skill
    from tools.skill_manager_tool import _find_skill
    from tools.skills_sync import _build_external_skill_index

    assert _find_skill("canonical") is None
    assert _check_unavailable_skill(PRIVATE_ID) is None
    assert PRIVATE_ID not in _build_external_skill_index()


def test_native_learning_graph_does_not_ingest_authority_skills(authority_env, tmp_path):
    external = tmp_path / "external"
    authority = external / "private"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(external, "public", "public-skill")
    _write_config(authority_env, external_dirs=[external], authority_roots=[authority])

    from agent.learning_graph import build_skill_nodes

    nodes = build_skill_nodes([("external", external)])

    assert PRIVATE_ID not in nodes
    assert "public-skill" in nodes


def test_native_usage_audit_excludes_authority_subtree(authority_env):
    local_skills = authority_env / "skills"
    authority = local_skills / "private"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(local_skills, "public", "public-skill")
    _write_config(authority_env, authority_roots=[authority])

    from tools.skill_usage import usage_report

    assert {row["name"] for row in usage_report()} == {"public-skill"}


def test_profile_reporting_does_not_count_or_describe_authority_skills(authority_env):
    local_skills = authority_env / "skills"
    authority = local_skills / "private"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(local_skills, "public", "public-skill")
    _write_config(authority_env, authority_roots=[authority])

    from hermes_cli.dump import _count_skills as count_dump_skills
    from hermes_cli.profile_describer import _collect_skills
    from hermes_cli.profiles import _count_skills as count_profile_skills

    assert count_profile_skills(authority_env) == 1
    assert count_dump_skills(authority_env) == 1
    assert _collect_skills(authority_env) == ["public"]


def test_local_adapter_and_noncentral_plugin_continue_to_route_and_load(authority_env, tmp_path, monkeypatch):
    authority = tmp_path / "authority"
    _write_skill(authority, "canonical", PRIVATE_ID)
    adapter = _write_skill(
        authority_env / "skills",
        "mcp-adapter",
        "mcp-adapter",
        body="Use the configured MCP tools for canonical workflows.",
    )
    plugin_skill = _write_skill(tmp_path / "plugin", "plugin-skill", "plugin-skill") / "SKILL.md"
    _write_config(authority_env, external_dirs=[authority], authority_roots=[authority])

    class Manager:
        def list_plugin_skill_metadata(self):
            return [
                {"name": "demo:plugin-skill", "description": "Plugin skill.", "path": plugin_skill},
                {"name": "authority:canonical", "description": "Private skill.", "path": authority / "canonical" / "SKILL.md"},
            ]

        def find_plugin_skill(self, name):
            if name == "demo:plugin-skill":
                return plugin_skill
            if name == "authority:canonical":
                return authority / "canonical" / "SKILL.md"
            return None

        def list_plugin_skills(self, namespace):
            if namespace == "demo":
                return ["plugin-skill"]
            if namespace == "authority":
                return ["canonical"]
            return []

    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: Manager())
    adapter_route = json.loads(skills_list(query="mcp-adapter", limit=3, budget_chars=10_000))
    plugin_route = json.loads(skills_list(query="plugin-skill", limit=3, budget_chars=10_000))

    assert json.loads(skill_view("mcp-adapter"))["success"] is True
    assert json.loads(skill_view("demo:plugin-skill"))["success"] is True
    assert json.loads(skill_view("authority:canonical")) == {
        "success": False,
        "error": "This skill is available only through its configured MCP authority.",
    }
    assert json.loads(skill_view("authority:unknown")) == {
        "success": False,
        "error": "This skill is available only through its configured MCP authority.",
    }
    assert [item["name"] for item in adapter_route["route"]] == ["mcp-adapter"]
    assert [item["name"] for item in plugin_route["route"]] == ["demo:plugin-skill"]
    assert adapter.exists()


def test_malformed_authority_root_fails_closed_without_path_diagnostic(authority_env, tmp_path, caplog):
    authority = tmp_path / "authority"
    _write_skill(authority, "canonical", PRIVATE_ID)
    alias = tmp_path / "authority-alias"
    alias.symlink_to(authority, target_is_directory=True)
    _write_config(authority_env, external_dirs=[authority], authority_roots=[alias])

    with caplog.at_level("WARNING"):
        roots = skill_utils.get_central_private_skill_roots()
        listing = skills_list()

    assert roots.diagnostics == ("central_private_root_invalid",)
    assert roots.roots == (authority.resolve(),)
    assert json.loads(listing)["skills"] == []
    assert str(alias) not in caplog.text
    assert str(authority) not in caplog.text


def test_scalar_authority_root_is_malformed_but_remains_excluded(authority_env, tmp_path, caplog):
    authority = tmp_path / "authority"
    _write_skill(authority, "canonical", PRIVATE_ID)
    (authority_env / "config.yaml").write_text(
        "skills:\n"
        f"  external_dirs:\n    - {authority}\n"
        f"  central_private_roots: {authority}\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        roots = skill_utils.get_central_private_skill_roots()

    assert roots.diagnostics == ("central_private_root_invalid",)
    assert roots.roots == (authority.resolve(),)
    assert json.loads(skills_list())["skills"] == []
    assert str(authority) not in caplog.text


def test_mcp_configuration_is_not_a_native_skill_source(authority_env, tmp_path):
    authority = tmp_path / "authority"
    _write_skill(authority, "canonical", PRIVATE_ID)
    _write_skill(authority_env / "skills", "mcp-adapter", "mcp-adapter")
    _write_config(
        authority_env,
        external_dirs=[authority],
        authority_roots=[authority],
        extra="mcp_servers:\n  canonical:\n    command: example-mcp",
    )

    from hermes_cli.config import load_config

    assert _names(skills_list()) == {"mcp-adapter"}
    assert json.loads(skill_view("mcp-adapter"))["success"] is True
    assert load_config()["mcp_servers"]["canonical"]["command"] == "example-mcp"
