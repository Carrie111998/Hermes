"""Production integration contracts for query-aware ``skills_list``."""

import json

import pytest

from tools.registry import registry
import tools.skills_tool as skills_tool


class _PluginManager:
    def __init__(self, skills):
        self.skills = skills

    def list_plugin_skill_metadata(self):
        return [dict(skill) for skill in self.skills]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    plugins = []
    skills_tool._SKILLS_CACHE.clear()
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    monkeypatch.setattr(skills_tool, "_get_disabled_skill_names", lambda: set())
    monkeypatch.setattr(skills_tool, "_is_skill_disabled", lambda name: False)
    monkeypatch.setattr(skills_tool, "skill_matches_platform", lambda metadata: True)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _PluginManager(plugins)
    )
    yield plugins
    skills_tool._SKILLS_CACHE.clear()


def _write_skill(root, name, *, description="public description", body="private body"):
    path = root / "skills" / "testing" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_environment_ineligible_plugin_is_omitted_in_both_modes(_isolated, monkeypatch):
    _isolated.extend([
        {
            "name": "demo:eligible",
            "description": "eligible needle",
            "category": "plugin",
            "frontmatter": {"environment": "eligible"},
        },
        {
            "name": "demo:private-secret-name",
            "description": "ineligible needle",
            "category": "plugin",
            "frontmatter": {"environment": "ineligible"},
        },
    ])
    monkeypatch.setattr(
        skills_tool,
        "skill_matches_environment",
        lambda metadata: metadata.get("environment") != "ineligible",
    )

    listing = json.loads(skills_tool.skills_list())
    ranking = json.loads(skills_tool.skills_list(query="needle"))

    assert [skill["name"] for skill in listing["skills"]] == ["demo:eligible"]
    assert [skill["name"] for skill in ranking["skills"]] == ["demo:eligible"]


def test_public_shapes_and_privacy_are_unchanged(tmp_path, monkeypatch):
    private_path = str(tmp_path / "private-location")
    private_body = "private-body-token"
    credential_value = "credential-value-token"
    environment_value = "environment-value-token"
    raw_query = "alpha raw-query-token"
    monkeypatch.setenv("ROUTING_SECRET", environment_value)
    _write_skill(tmp_path, "alpha", body=private_body)

    listing = json.loads(skills_tool.skills_list())
    ranking = json.loads(skills_tool.skills_list(query=raw_query))
    serialized = json.dumps({"listing": listing, "ranking": ranking})

    assert list(listing) == ["success", "skills", "categories", "count", "hint"]
    assert set(listing["skills"][0]) == {"name", "description", "category"}
    assert list(ranking) == [
        "success",
        "mode",
        "skills",
        "count",
        "total_candidates",
        "index_fingerprint",
        "hint",
    ]
    assert set(ranking["skills"][0]) == {
        "rank",
        "name",
        "category",
        "description",
        "score",
    }
    for private in (
        private_path,
        private_body,
        credential_value,
        environment_value,
        raw_query,
        "source_fingerprint",
        "_routing",
    ):
        assert private not in serialized
    assert registry.get_entry("skills_list") is not None


def test_ranking_does_not_load_skill_body(tmp_path, monkeypatch):
    _write_skill(tmp_path, "alpha", body="body-only-secret")
    called = False

    def forbidden_view(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("skill_view must remain the explicit loading entrypoint")

    monkeypatch.setattr(skills_tool, "skill_view", forbidden_view)
    result = json.loads(skills_tool.skills_list(query="alpha"))

    assert result["skills"][0]["name"] == "alpha"
    assert called is False
