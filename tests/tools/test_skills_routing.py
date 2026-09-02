"""Focused integration tests for BM25 routing through ``skills_list``."""

import inspect
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
def _isolated_catalog(tmp_path, monkeypatch):
    plugin_skills = []
    skills_tool._SKILLS_CACHE.clear()
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    monkeypatch.setattr(skills_tool, "_get_disabled_skill_names", lambda: set())
    monkeypatch.setattr(skills_tool, "_is_skill_disabled", lambda name: False)
    monkeypatch.setattr(skills_tool, "skill_matches_platform", lambda metadata: True)
    monkeypatch.setattr(skills_tool, "skill_matches_environment", lambda metadata: True)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _PluginManager(plugin_skills),
    )
    yield plugin_skills
    skills_tool._SKILLS_CACHE.clear()


def _write_skill(
    root,
    name,
    *,
    category=None,
    description=None,
    frontmatter="",
    body="Step 1: Do the thing.",
):
    skill_dir = root / "skills"
    if category:
        skill_dir /= category
    skill_dir /= name
    skill_dir.mkdir(parents=True, exist_ok=True)
    description = description or f"Description for {name}."
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{frontmatter}"
        "---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_no_query_output_remains_byte_compatible(tmp_path):
    _write_skill(tmp_path, "alpha", category="testing")

    actual = skills_tool.skills_list()

    expected = json.dumps(
        {
            "success": True,
            "skills": [
                {
                    "name": "alpha",
                    "description": "Description for alpha.",
                    "category": "testing",
                }
            ],
            "categories": ["testing"],
            "count": 1,
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        },
        ensure_ascii=False,
    )
    assert actual == expected


def test_whitespace_query_preserves_listing_even_with_invalid_limit(tmp_path):
    _write_skill(tmp_path, "alpha", category="testing")

    assert (
        skills_tool.skills_list(query=" \t\n", limit=False) == skills_tool.skills_list()
    )


def test_malformed_routing_metadata_does_not_remove_eligible_skill(tmp_path):
    _write_skill(
        tmp_path,
        "alpha",
        category="testing",
        frontmatter="prerequisites:\n  commands: 1\n",
    )

    listing = json.loads(skills_tool.skills_list())
    ranking = json.loads(skills_tool.skills_list(query="alpha"))

    assert [skill["name"] for skill in listing["skills"]] == ["alpha"]
    assert [skill["name"] for skill in ranking["skills"]] == ["alpha"]
    assert ranking["total_candidates"] == 1


def test_query_mode_is_compact_path_free_and_does_not_index_body(tmp_path):
    _write_skill(
        tmp_path,
        "zeta",
        category="testing",
        body="This full body contains bodyneedle but must not be indexed.",
    )
    _write_skill(tmp_path, "alpha", category="testing")

    result = json.loads(skills_tool.skills_list(query="bodyneedle", limit=2))

    assert list(result) == [
        "success",
        "mode",
        "skills",
        "count",
        "total_candidates",
        "index_fingerprint",
        "hint",
    ]
    assert result["success"] is True
    assert result["mode"] == "bm25"
    assert [skill["name"] for skill in result["skills"]] == ["alpha", "zeta"]
    assert set(result["skills"][0]) == {
        "rank",
        "name",
        "category",
        "description",
        "score",
    }
    assert result["count"] == 2
    assert result["total_candidates"] == 2
    assert len(result["index_fingerprint"]) == 16
    assert "skill_view(name)" in result["hint"]
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert "bodyneedle" not in serialized
    assert "query" not in result


def test_query_does_not_index_body_fallback_for_missing_description(tmp_path):
    skill_dir = tmp_path / "skills" / "testing" / "zeta"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: zeta\n"
        "---\n\n"
        "# zeta\n\n"
        "bodyfallbackneedle must remain display-only.\n",
        encoding="utf-8",
    )
    _write_skill(tmp_path, "alpha", category="testing")

    result = json.loads(skills_tool.skills_list(query="bodyfallbackneedle", limit=2))

    assert [skill["name"] for skill in result["skills"]] == ["alpha", "zeta"]
    assert [skill["score"] for skill in result["skills"]] == [0.0, 0.0]
    assert result["skills"][1]["description"] == (
        "bodyfallbackneedle must remain display-only."
    )


@pytest.mark.parametrize(
    "query",
    [
        "triggerneedle",
        "tagneedle",
        "relatedneedle",
        "commandneedle",
        "envneedle",
        "legacycommandneedle",
        "legacyenvneedle",
        "descriptionneedle",
    ],
)
def test_query_indexes_declared_routing_metadata(tmp_path, query):
    long_description = "x" * 1050 + " descriptionneedle"
    _write_skill(
        tmp_path,
        "zeta",
        category="testing",
        description=long_description,
        frontmatter=(
            "triggers: [triggerneedle]\n"
            "metadata:\n"
            "  hermes:\n"
            "    tags: [tagneedle]\n"
            "    related_skills: [relatedneedle]\n"
            "required_commands: [commandneedle]\n"
            "required_environment_variables:\n"
            "  - name: ENVNEEDLE\n"
            "prerequisites:\n"
            "  commands: [legacycommandneedle]\n"
            "  env_vars: [LEGACYENVNEEDLE]\n"
        ),
    )
    _write_skill(tmp_path, "alpha", category="testing")

    result = json.loads(skills_tool.skills_list(query=query, limit=2))

    assert result["skills"][0]["name"] == "zeta"
    assert result["skills"][0]["score"] > 0


def test_query_applies_category_filter_before_ranking(tmp_path):
    _write_skill(
        tmp_path, "devops-match", category="devops", frontmatter="tags: [needle]\n"
    )
    _write_skill(
        tmp_path, "mlops-match", category="mlops", frontmatter="tags: [needle]\n"
    )
    _write_skill(tmp_path, "devops-other", category="devops")

    result = json.loads(
        skills_tool.skills_list(category="devops", query="needle", limit=8)
    )

    assert {skill["name"] for skill in result["skills"]} == {"devops-match"}
    assert result["total_candidates"] == 2


def test_query_includes_eligible_plugin_skill_metadata(_isolated_catalog):
    _isolated_catalog.append({
        "name": "demo:router",
        "description": "Routes generic work",
        "category": "plugin",
        "frontmatter": {"metadata": {"hermes": {"tags": ["pluginneedle"]}}},
    })

    result = json.loads(skills_tool.skills_list(query="pluginneedle"))

    assert result["skills"][0]["name"] == "demo:router"
    assert result["skills"][0]["category"] == "plugin"
    assert result["total_candidates"] == 1


@pytest.mark.parametrize("limit", [True, False, 0, -1, 51, 1.5, "2"])
def test_query_rejects_invalid_limit(limit):
    result = json.loads(skills_tool.skills_list(query="needle", limit=limit))

    assert result == {
        "error": "limit must be an integer between 1 and 50",
        "success": False,
    }


def test_query_honors_default_and_explicit_limit(tmp_path):
    for index in range(10):
        _write_skill(tmp_path, f"skill-{index:02d}", category="testing")

    default = json.loads(skills_tool.skills_list(query="unmatched"))
    explicit = json.loads(skills_tool.skills_list(query="unmatched", limit=3))

    assert default["count"] == 0
    assert explicit["count"] == 3
    assert default["total_candidates"] == explicit["total_candidates"] == 10


def test_signature_schema_and_handler_expose_query_limit_and_task_id(monkeypatch):
    signature = inspect.signature(skills_tool.skills_list)
    assert list(signature.parameters) == ["category", "query", "limit", "task_id"]
    assert signature.parameters["query"].default is None
    assert signature.parameters["limit"].default == 8
    assert signature.parameters["task_id"].default is None

    properties = skills_tool.SKILLS_LIST_SCHEMA["parameters"]["properties"]
    assert properties["query"]["type"] == "string"
    assert properties["limit"] == {
        "type": "integer",
        "description": "Maximum ranked skills to return in query mode",
        "default": 8,
        "minimum": 1,
        "maximum": 50,
    }

    captured = {}

    def fake_skills_list(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(skills_tool, "skills_list", fake_skills_list)
    entry = registry.get_entry("skills_list")
    assert (
        entry.handler(
            {"category": "testing", "query": "needle", "limit": 3},
            task_id="task-123",
        )
        == "ok"
    )
    assert captured == {
        "category": "testing",
        "query": "needle",
        "limit": 3,
        "task_id": "task-123",
    }
