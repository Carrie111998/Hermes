"""Tests for tools/skills_tool.py — skill discovery and viewing."""

import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.skills_tool as skills_tool_module
from tools.skills_tool import (
    _get_required_environment_variables,
    _parse_frontmatter,
    _parse_tags,
    _get_category_from_path,
    _find_all_skills,
    skill_matches_platform,
    skills_list,
    skill_view,
    MAX_DESCRIPTION_LENGTH,
    MAX_LINKED_FILES_PER_CATEGORY,
)


def _make_skill(
    skills_dir, name, frontmatter_extra="", body="Step 1: Do the thing.", category=None
):
    """Helper to create a minimal skill directory."""
    if category:
        skill_dir = skills_dir / category / name
    else:
        skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
name: {name}
description: Description for {name}.
{frontmatter_extra}---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


def _symlink_category(skills_dir: Path, linked_root: Path, category: str) -> Path:
    """Create a category symlink under skills_dir pointing outside the tree."""
    external_category = linked_root / category
    external_category.mkdir(parents=True, exist_ok=True)
    symlink_path = skills_dir / category
    try:
        symlink_path.symlink_to(external_category, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")
    return external_category


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_and_nested_frontmatter(self):
        content = "---\nname: test\ndescription: A test.\n---\n\n# Body\n"
        fm, body = _parse_frontmatter(content)
        assert fm["name"] == "test"
        assert fm["description"] == "A test."
        assert "# Body" in body

        nested = "---\nname: test\nmetadata:\n  hermes:\n    tags: [a, b]\n---\n\nBody.\n"
        fm, _ = _parse_frontmatter(nested)
        assert fm["metadata"]["hermes"]["tags"] == ["a", "b"]


    def test_utf8_bom_frontmatter(self):
        """A leading UTF-8 BOM (Windows Notepad / PowerShell ``>`` save) must
        not drop the frontmatter. Confirms the fix reaches the tools/ surface
        via the _parse_frontmatter re-export."""
        bom = chr(0xFEFF)
        content = bom + "---\nname: test\ndescription: A test.\n---\n\n# Body\n"
        fm, body = _parse_frontmatter(content)
        assert fm["name"] == "test"
        assert fm["description"] == "A test."
        assert not body.startswith(bom)


# ---------------------------------------------------------------------------
# _parse_tags
# ---------------------------------------------------------------------------


class TestParseTags:
    def test_accepted_input_forms(self):
        assert _parse_tags(["a", "b", "c"]) == ["a", "b", "c"]
        assert _parse_tags("a, b, c") == ["a", "b", "c"]
        assert _parse_tags("[a, b, c]") == ["a", "b", "c"]
        # Quotes are stripped.
        result = _parse_tags("\"tag1\", 'tag2'")
        assert "tag1" in result
        assert "tag2" in result

    def test_empty_and_blank_items_dropped(self):
        assert _parse_tags("") == []
        assert _parse_tags(None) == []
        assert _parse_tags([]) == []
        assert _parse_tags([None, "", "valid"]) == ["valid"]


class TestRequiredEnvironmentVariablesNormalization:
    def test_parses_new_metadata_and_normalizes_legacy_prerequisites(self):
        frontmatter = {
            "required_environment_variables": [
                {
                    "name": "TENOR_API_KEY",
                    "prompt": "Tenor API key",
                    "help": "Get a key from https://developers.google.com/tenor",
                    "required_for": "full functionality",
                }
            ]
        }
        assert _get_required_environment_variables(frontmatter) == [
            {
                "name": "TENOR_API_KEY",
                "prompt": "Tenor API key",
                "help": "Get a key from https://developers.google.com/tenor",
                "required_for": "full functionality",
            }
        ]

        legacy = {"prerequisites": {"env_vars": ["TENOR_API_KEY"]}}
        assert _get_required_environment_variables(legacy) == [
            {
                "name": "TENOR_API_KEY",
                "prompt": "Enter value for TENOR_API_KEY",
            }
        ]

    def test_empty_env_file_value_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("FILLED_KEY", "value")
        monkeypatch.setenv("EMPTY_HOST_KEY", "")

        from tools.skills_tool import _is_env_var_persisted

        assert _is_env_var_persisted("EMPTY_FILE_KEY", {"EMPTY_FILE_KEY": ""}) is False
        assert (
            _is_env_var_persisted("FILLED_FILE_KEY", {"FILLED_FILE_KEY": "x"}) is True
        )
        assert _is_env_var_persisted("EMPTY_HOST_KEY", {}) is False
        assert _is_env_var_persisted("FILLED_KEY", {}) is True


# ---------------------------------------------------------------------------
# _get_category_from_path
# ---------------------------------------------------------------------------


class TestGetCategoryFromPath:
    def test_category_derived_from_layout(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            categorized = tmp_path / "mlops" / "axolotl" / "SKILL.md"
            categorized.parent.mkdir(parents=True)
            categorized.touch()
            assert _get_category_from_path(categorized) == "mlops"

            top_level = tmp_path / "my-skill" / "SKILL.md"
            top_level.parent.mkdir(parents=True)
            top_level.touch()
            assert _get_category_from_path(top_level) is None

        # Paths outside SKILLS_DIR have no category.
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path / "skills"):
            assert _get_category_from_path(tmp_path / "other" / "SKILL.md") is None


# ---------------------------------------------------------------------------
# _find_all_skills
# ---------------------------------------------------------------------------


class TestFindAllSkills:
    def test_finds_skills_and_skips_non_skill_trees(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "skill-a")
            _make_skill(tmp_path, "skill-b")
            skills = _find_all_skills()
        assert len(skills) == 2
        names = {s["name"] for s in skills}
        assert "skill-a" in names
        assert "skill-b" in names

    def test_empty_directory(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skills = _find_all_skills()
        assert skills == []

    @pytest.mark.parametrize(
        "broken",
        (
            "---\nname: steals-valid\n# no closing fence\n",
            "---\n- steals-valid\n---\n",
        ),
        ids=("unclosed-fence", "non-mapping-yaml"),
    )
    def test_list_refuses_partial_catalog_for_invalid_fenced_skill(self, tmp_path, broken):
        _make_skill(tmp_path, "valid")
        corrupt = _make_skill(tmp_path, "corrupt") / "SKILL.md"
        corrupt.write_text(broken, encoding="utf-8")

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = json.loads(skills_list())

        assert result["success"] is True
        assert result["skills"] == []

    def test_list_and_view_reject_canonical_skill_symlink(self, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: escaped\ndescription: outside\n---\n", encoding="utf-8"
        )
        skill_dir = tmp_path / "escaped"
        skill_dir.mkdir()
        try:
            (skill_dir / "SKILL.md").symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            listed = json.loads(skills_list())
            viewed = json.loads(skill_view("escaped"))

        assert listed["success"] is True
        assert listed["skills"] == []
        assert viewed["success"] is False
        assert viewed.get("error_code") == "skills_discovery_incomplete"

    def test_nonexistent_directory(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path / "nope"):
            skills = _find_all_skills()
        assert skills == []

    def test_categorized_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "axolotl", category="mlops")

            # .git internals are not skills.
            git_dir = tmp_path / ".git" / "fake-skill"
            git_dir.mkdir(parents=True)
            (git_dir / "SKILL.md").write_text(
                "---\nname: fake\ndescription: x\n---\n\nBody.\n"
            )
            # Neither are skills vendored inside a nested virtualenv.
            typer_skill = (
                tmp_path
                / "bring"
                / "scripts"
                / ".venv"
                / "lib"
                / "python3.13"
                / "site-packages"
                / "typer"
                / ".agents"
                / "skills"
                / "typer"
            )
            typer_skill.mkdir(parents=True)
            (typer_skill / "SKILL.md").write_text(
                "---\nname: typer\ndescription: Should not be discovered.\n---\n",
                encoding="utf-8",
            )

            skills = _find_all_skills()

        assert {s["name"] for s in skills} == {"axolotl"}
        assert skills[0]["category"] == "mlops"


    def test_description_falls_back_to_body_and_is_truncated(self, tmp_path):
        no_desc = tmp_path / "no-desc"
        no_desc.mkdir()
        (no_desc / "SKILL.md").write_text(
            "---\nname: no-desc\n---\n\n# Heading\n\nFirst paragraph.\n"
        )
        long_dir = tmp_path / "long-desc"
        long_dir.mkdir()
        (long_dir / "SKILL.md").write_text(
            f"---\nname: long\ndescription: {'x' * (MAX_DESCRIPTION_LENGTH + 100)}\n---\n\nBody.\n"
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skills = {s["name"]: s for s in _find_all_skills()}

        # If no description in frontmatter, the first non-header line is used.
        assert skills["no-desc"]["description"] == "First paragraph."
        assert len(skills["long"]["description"]) <= MAX_DESCRIPTION_LENGTH

    def test_finds_skills_in_symlinked_category_dir(self, tmp_path):
        external_root = tmp_path / "repo"
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        external_category = _symlink_category(skills_root, external_root, "linked")
        _make_skill(external_category.parent, "knowledge-brain", category="linked")

        with patch("tools.skills_tool.SKILLS_DIR", skills_root):
            skills = _find_all_skills()

        assert [s["name"] for s in skills] == ["knowledge-brain"]
        assert skills[0]["category"] == "linked"

    def test_cache_invalidates_when_kanban_environment_changes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "tools.kanban_tools._profile_has_kanban_toolset",
                return_value=False,
            ),
        ):
            _make_skill(
                tmp_path,
                "kanban-only",
                frontmatter_extra="environments: [kanban]\n",
            )
            skills_tool_module._SKILLS_CACHE.clear()
            assert _find_all_skills() == []

            monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
            assert [s["name"] for s in _find_all_skills()] == ["kanban-only"]

            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
            assert _find_all_skills() == []

    def test_read_error_returns_same_scope_last_good_not_partial(
        self, tmp_path, monkeypatch
    ):
        """A selected file can fail after walk; retain its same-root catalog."""
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skills_tool_module._SKILLS_CACHE.clear()
            _make_skill(tmp_path, "good")
            assert [s["name"] for s in _find_all_skills()] == ["good"]

            bad_file = _make_skill(tmp_path, "bad") / "SKILL.md"
            original_reader = skills_tool_module.read_strict_skill_index_file

            def fail_only_bad_file(path, *args, **kwargs):
                if path == bad_file:
                    raise OSError("simulated file read denial")
                return original_reader(path, *args, **kwargs)

            monkeypatch.setattr(
                skills_tool_module, "read_strict_skill_index_file", fail_only_bad_file
            )
            monkeypatch.setattr(skills_tool_module, "_SKILLS_CACHE_TTL_SECONDS", 0)
            assert [s["name"] for s in _find_all_skills()] == ["good"]

    def test_stat_error_does_not_commit_partial_catalog(self, tmp_path, monkeypatch):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skills_tool_module._SKILLS_CACHE.clear()
            _make_skill(tmp_path, "good")
            assert [s["name"] for s in _find_all_skills()] == ["good"]

            bad_file = _make_skill(tmp_path, "bad") / "SKILL.md"
            original_reader = skills_tool_module.read_strict_skill_index_file

            def fail_only_bad_file(path, *args, **kwargs):
                if path == bad_file:
                    raise OSError("simulated file stat denial")
                return original_reader(path, *args, **kwargs)

            monkeypatch.setattr(
                skills_tool_module, "read_strict_skill_index_file", fail_only_bad_file
            )
            monkeypatch.setattr(skills_tool_module, "_SKILLS_CACHE_TTL_SECONDS", 0)
            assert [s["name"] for s in _find_all_skills()] == ["good"]

    def test_walk_error_does_not_leak_another_scope_cache(self, tmp_path, monkeypatch):
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        first_root.mkdir()
        second_root.mkdir()
        with patch("tools.skills_tool.SKILLS_DIR", first_root):
            skills_tool_module._SKILLS_CACHE.clear()
            _make_skill(first_root, "first-only")
            assert [s["name"] for s in _find_all_skills()] == ["first-only"]

        def denied_walk(*_args, **kwargs):
            kwargs["onerror"](OSError("simulated walk denial"))
            return []

        with patch("tools.skills_tool.SKILLS_DIR", second_root):
            monkeypatch.setattr("agent.skill_utils.os.walk", denied_walk)
            assert _find_all_skills() == []

    def test_active_root_stat_error_does_not_cache_external_only_result(
        self, tmp_path, monkeypatch
    ):
        """An unreadable local root is not equivalent to an empty local root."""
        active_root = tmp_path / "active"
        external_root = tmp_path / "external"
        active_root.mkdir()
        external_root.mkdir()
        _make_skill(active_root, "local-skill")
        _make_skill(external_root, "external-skill")
        original_stat = Path.stat

        with (
            patch("tools.skills_tool.SKILLS_DIR", active_root),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[external_root],
            ),
        ):
            skills_tool_module._SKILLS_CACHE.clear()
            assert sorted(s["name"] for s in _find_all_skills()) == [
                "external-skill",
                "local-skill",
            ]
            monkeypatch.setattr(
                skills_tool_module, "_SKILLS_CACHE_TTL_SECONDS", 0
            )

            def fail_only_active_root(path, *args, **kwargs):
                if path == active_root:
                    raise PermissionError("simulated active-root denial")
                return original_stat(path, *args, **kwargs)

            monkeypatch.setattr(Path, "stat", fail_only_active_root)
            # The same full-scope last-good catalog is retained; the scan must
            # not replace it with the external-only subset.
            assert sorted(s["name"] for s in _find_all_skills()) == [
                "external-skill",
                "local-skill",
            ]


# ---------------------------------------------------------------------------
# skills_list
# ---------------------------------------------------------------------------


class TestSkillsList:
    def test_empty_creates_directory(self, tmp_path):
        skills_dir = tmp_path / "skills"
        with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
            raw = skills_list()
        result = json.loads(raw)
        assert result["success"] is True
        assert result["skills"] == []
        assert skills_dir.exists()

    @pytest.mark.parametrize("root_kind", ["missing", "file"])
    def test_invalid_configured_external_root_returns_structured_error(
        self, tmp_path, root_kind
    ):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "local-only")
        external_root = tmp_path / "configured-external"
        if root_kind == "file":
            external_root.write_text("not a directory", encoding="utf-8")

        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_dir),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[external_root],
            ),
        ):
            skills_tool_module._SKILLS_CACHE.clear()
            result = json.loads(skills_list())

        assert result["success"] is False
        assert result["error_code"] == "skills_discovery_incomplete"
        assert "local-only" not in json.dumps(result)
        assert skills_tool_module._SKILLS_CACHE == {}

    def test_lists_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "alpha")
            _make_skill(tmp_path, "beta")
            raw = skills_list()
        result = json.loads(raw)
        assert result["count"] == 2

    def test_category_filter(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "skill-a", category="devops")
            _make_skill(tmp_path, "skill-b", category="mlops")
            all_result = json.loads(skills_list())
            filtered = json.loads(skills_list(category="devops"))

        assert all_result["count"] == 2
        assert filtered["count"] == 1
        assert filtered["skills"][0]["name"] == "skill-a"

    def test_category_filter_finds_symlinked_category(self, tmp_path):
        external_root = tmp_path / "repo"
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        external_category = _symlink_category(skills_root, external_root, "linked")
        _make_skill(external_category.parent, "knowledge-brain", category="linked")

        with patch("tools.skills_tool.SKILLS_DIR", skills_root):
            raw = skills_list(category="linked")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["count"] == 1
        assert result["categories"] == ["linked"]
        assert result["skills"][0]["name"] == "knowledge-brain"


# ---------------------------------------------------------------------------
# skill_view
# ---------------------------------------------------------------------------


class TestSkillView:
    def test_view_resolves_by_dir_name_and_frontmatter_name(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "my-skill")
            raw = skill_view("my-skill")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["name"] == "my-skill"
        assert "Step 1" in result["content"]

    def test_missing_configured_external_root_blocks_ambiguous_local_lookup(
        self, tmp_path
    ):
        local_root = tmp_path / "skills"
        missing_external = tmp_path / "configured-external"
        _make_skill(local_root, "possibly-colliding")

        with (
            patch("tools.skills_tool.SKILLS_DIR", local_root),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[missing_external],
            ),
        ):
            result = json.loads(skill_view("possibly-colliding"))

        assert result["success"] is False
        assert result["error_code"] == "skills_discovery_incomplete"
        assert "Step 1" not in json.dumps(result)

    def test_view_skill_by_frontmatter_name_when_dir_differs(self, tmp_path):
        # The on-disk directory ("alias-dir") differs from the skill's
        # frontmatter name ("real-skill-name"). skills_list() exposes the
        # frontmatter name, so skill_view(name) must resolve it too.
        skill_dir = tmp_path / "alias-dir"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: real-skill-name\n"
            "description: A skill whose directory name differs from its name.\n"
            "---\n\n"
            "# real-skill-name\n\n"
            "Step 1: Do the thing.\n"
        )
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view("real-skill-name")
        result = json.loads(raw)
        assert result["success"] is True
        assert "Step 1" in result["content"]

    def test_frontmatter_like_body_prefix_is_not_treated_as_a_fence(self, tmp_path):
        skill_dir = tmp_path / "plain-body"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---not-a-fence\n\nKeep this as ordinary body text.\n",
            encoding="utf-8",
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = json.loads(skill_view("plain-body"))

        assert result["success"] is True
        assert "---not-a-fence" in result["content"]

    def test_skill_view_applies_template_vars(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={"template_vars": True, "inline_shell": False},
            ),
        ):
            skill_dir = _make_skill(
                tmp_path,
                "templated",
                body="Run ${HERMES_SKILL_DIR}/scripts/do.sh in ${HERMES_SESSION_ID}",
            )
            raw = skill_view("templated", task_id="session-123")

        result = json.loads(raw)
        assert result["success"] is True
        assert f"Run {skill_dir}/scripts/do.sh in session-123" in result["content"]
        assert "${HERMES_SKILL_DIR}" not in result["content"]

    def test_skill_view_applies_inline_shell_when_enabled(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_preprocessing.load_skills_config",
                return_value={
                    "template_vars": True,
                    "inline_shell": True,
                    "inline_shell_timeout": 5,
                },
            ),
        ):
            _make_skill(
                tmp_path,
                "my-skill",
                frontmatter_extra="metadata:\n  hermes:\n    tags: [fine-tuning, llm]\n",
            )
            # The on-disk directory ("alias-dir") differs from the skill's
            # frontmatter name ("real-skill-name"). skills_list() exposes the
            # frontmatter name, so skill_view(name) must resolve it too.
            alias_dir = tmp_path / "alias-dir"
            alias_dir.mkdir(parents=True, exist_ok=True)
            (alias_dir / "SKILL.md").write_text(
                "---\n"
                "name: real-skill-name\n"
                "description: A skill whose directory name differs from its name.\n"
                "---\n\n"
                "# real-skill-name\n\n"
                "Step 1: Do the thing.\n"
            )
            by_dir = json.loads(skill_view("my-skill"))
            by_name = json.loads(skill_view("real-skill-name"))

        assert by_dir["success"] is True
        assert by_dir["name"] == "my-skill"
        assert "Step 1" in by_dir["content"]
        assert "fine-tuning" in by_dir["tags"]
        assert "llm" in by_dir["tags"]

        assert by_name["success"] is True
        assert "Step 1" in by_name["content"]
    def test_inline_shell_cwd_remains_bound_across_category_symlink_swap(
        self, tmp_path, monkeypatch
    ):
        skills_root = tmp_path / "skills"
        packages = tmp_path / "packages"
        original_category = packages / "original"
        replacement_category = packages / "replacement"
        skills_root.mkdir()
        original_skill = _make_skill(
            original_category,
            "dynamic",
            body="Marker: !`cat marker.txt`",
        )
        replacement_skill = replacement_category / "dynamic"
        replacement_skill.mkdir(parents=True)
        os.link(
            original_skill / "SKILL.md",
            replacement_skill / "SKILL.md",
        )
        (original_skill / "marker.txt").write_text("ORIGINAL", encoding="utf-8")
        (replacement_skill / "marker.txt").write_text(
            "REPLACEMENT", encoding="utf-8"
        )
        category_link = skills_root / "linked"
        category_link.symlink_to(original_category, target_is_directory=True)
        swapped = False

        def swap_while_loading_config():
            nonlocal swapped
            if not swapped:
                swapped = True
                category_link.unlink()
                category_link.symlink_to(
                    replacement_category, target_is_directory=True
                )
            return {
                "template_vars": True,
                "inline_shell": True,
                "inline_shell_timeout": 5,
            }

        monkeypatch.setattr(
            "agent.skill_preprocessing.load_skills_config",
            swap_while_loading_config,
        )
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[],
            ),
        ):
            result = json.loads(skill_view("dynamic"))

        assert swapped is True
        assert result["success"] is True
        assert "Marker: ORIGINAL" in result["content"]
        assert "REPLACEMENT" not in result["content"]

    def test_windows_inline_shell_rewrites_only_commands_to_bound_snapshot(
        self, tmp_path
    ):
        from tools import skills_tool
        from tools import nt_secure_fs_optional as nt_secure_fs

        original = Path("C:/skills/dynamic")
        handle = MagicMock()
        handle.final_path.return_value = original
        content = (
            "Visible path: ${HERMES_SKILL_DIR}\n"
            "Marker: !`cat ${HERMES_SKILL_DIR}/marker.txt`"
        )

        def materialize(_handle, destination):
            assert str(destination).replace("\\", "/").endswith(
                "C:/Temp/inline/skill"
            )

        def expand(bound_content, cwd, timeout):
            assert timeout == 5
            assert str(original) in bound_content.splitlines()[0]
            command = bound_content.split("!`", 1)[1].split("`", 1)[0]
            assert str(original) not in command
            assert "\\" not in command
            assert "C:/Temp/inline/skill" in command
            return bound_content.replace(f"!`{command}`", "ORIGINAL")

        with (
            patch.object(
                skills_tool, "os", SimpleNamespace(name="nt")
            ),
            patch(
                "tools.skills_tool.tempfile.TemporaryDirectory"
            ) as temp_dir,
            patch.object(
                nt_secure_fs,
                "copy_tree_no_reparse",
                side_effect=materialize,
            ),
            patch(
                "agent.skill_preprocessing.expand_inline_shell",
                side_effect=expand,
            ),
        ):
            temp_dir.return_value.__enter__.return_value = (
                r"C:\Temp\inline"
            )
            temp_dir.return_value.__exit__.return_value = False
            result = skills_tool._expand_inline_shell_bound(
                content,
                handle,
                5,
                skill_dir=original,
            )

        assert f"Visible path: {original}" in result
        assert "Marker: ORIGINAL" in result

    def test_posix_inline_shell_template_path_stays_on_held_package(
        self, tmp_path
    ):
        from tools import skills_tool

        package_path = tmp_path / "dynamic"
        package_path.mkdir()
        (package_path / "marker.txt").write_text(
            "ORIGINAL", encoding="utf-8"
        )
        package_fd = os.open(
            package_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        moved_path = tmp_path / "moved"
        package_path.rename(moved_path)
        package_path.mkdir()
        (package_path / "marker.txt").write_text(
            "REPLACEMENT", encoding="utf-8"
        )
        try:
            result = skills_tool._expand_inline_shell_bound(
                (
                    "Visible: ${HERMES_SKILL_DIR}\n"
                    f"Marker: !`cd {package_path} && "
                    "cat marker.txt`"
                ),
                package_fd,
                5,
                skill_dir=package_path,
            )
        finally:
            os.close(package_fd)

        assert f"Visible: {package_path}" in result
        assert "Marker: ORIGINAL" in result
        assert "REPLACEMENT" not in result

    def test_inline_shell_template_path_with_spaces_is_shell_safe(
        self, tmp_path, monkeypatch
    ):
        from tools import skills_tool

        package_path = tmp_path / "dynamic"
        package_path.mkdir()
        (package_path / "marker.txt").write_text(
            "ORIGINAL", encoding="utf-8"
        )
        temp_base = tmp_path / "snapshot base with spaces"
        temp_base.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_base))
        package_fd = os.open(
            package_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            result = skills_tool._expand_inline_shell_bound(
                (
                    "Marker: "
                    "!`cat ${HERMES_SKILL_DIR}/marker.txt`"
                ),
                package_fd,
                5,
                skill_dir=package_path,
            )
        finally:
            os.close(package_fd)

        assert result == "Marker: ORIGINAL"

    def test_inline_shell_literal_path_with_spaces_is_shell_safe(
        self, tmp_path, monkeypatch
    ):
        from tools import skills_tool

        package_path = tmp_path / "dynamic"
        package_path.mkdir()
        (package_path / "marker.txt").write_text(
            "ORIGINAL", encoding="utf-8"
        )
        temp_base = tmp_path / "snapshot base with spaces"
        temp_base.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temp_base))
        package_fd = os.open(
            package_path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            result = skills_tool._expand_inline_shell_bound(
                (
                    f"Marker: !`cd {package_path} && "
                    "cat marker.txt`"
                ),
                package_fd,
                5,
                skill_dir=package_path,
            )
        finally:
            os.close(package_fd)

        assert result == "Marker: ORIGINAL"

    def test_view_nonexistent_skill(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "other-skill")
            raw = skill_view("nonexistent")
        result = json.loads(raw)
        assert result["success"] is False
        assert "not found" in result["error"].lower()
        assert "available_skills" in result


    def test_view_reference_files(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "my-skill")
            refs_dir = skill_dir / "references"
            refs_dir.mkdir()
            (refs_dir / "api.md").write_text(
                "# API Docs\nEndpoint info.", encoding="utf-8"
            )

            existing = json.loads(skill_view("my-skill", file_path="references/api.md"))
            skill = json.loads(skill_view("my-skill"))

        assert existing["success"] is True
        assert "Endpoint info" in existing["content"]
        # The skill view advertises what else can be opened.
        assert skill["linked_files"] is not None
        assert "references" in skill["linked_files"]

    def test_view_nonexistent_file(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "my-skill")
            refs_dir = skill_dir / "references"
            refs_dir.mkdir()
            for index in reversed(range(MAX_LINKED_FILES_PER_CATEGORY + 5)):
                (refs_dir / f"{index:03d}.md").write_text(
                    "reference", encoding="utf-8"
                )
            raw = skill_view("my-skill", file_path="references/nope.md")
        result = json.loads(raw)
        assert result["success"] is False
        assert (
            len(result["available_files"]["references"])
            == MAX_LINKED_FILES_PER_CATEGORY
        )
        assert result["available_files"]["references"] == sorted(
            result["available_files"]["references"]
        )
        assert result["linked_files_summary"]["truncated"] is True

    def test_disabled_skill_blocked_enabled_allowed(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._is_skill_disabled", return_value=True),
        ):
            _make_skill(tmp_path, "hidden-skill")
            blocked = json.loads(skill_view("hidden-skill"))
        assert blocked["success"] is False
        assert "disabled" in blocked["error"].lower()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._is_skill_disabled", return_value=False),
        ):
            _make_skill(tmp_path, "active-skill")
            allowed = json.loads(skill_view("active-skill"))
        assert allowed["success"] is True

    def test_view_bounds_and_sorts_linked_file_preview(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "many-assets")
            assets_dir = skill_dir / "assets"
            assets_dir.mkdir()
            for index in reversed(range(MAX_LINKED_FILES_PER_CATEGORY + 5)):
                (assets_dir / f"{index:03d}.txt").write_text(
                    "asset", encoding="utf-8"
                )
            raw = skill_view("many-assets")

        result = json.loads(raw)
        shown = result["linked_files"]["assets"]
        summary = result["linked_files_summary"]
        assert len(shown) == MAX_LINKED_FILES_PER_CATEGORY
        assert shown == sorted(shown)
        assert summary["truncated"] is True
        assert summary["truncated_categories"] == ["assets"]

        # A file outside the preview remains explicitly readable.
        omitted = f"assets/{MAX_LINKED_FILES_PER_CATEGORY + 4:03d}.txt"
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            explicit = json.loads(skill_view("many-assets", file_path=omitted))
        assert explicit["success"] is True
        assert explicit["content"] == "asset"

    def test_view_does_not_follow_linked_file_category_symlink(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("outside", encoding="utf-8")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _make_skill(tmp_path, "linked-assets")
            try:
                (skill_dir / "assets").symlink_to(
                    outside, target_is_directory=True
                )
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlinks unavailable: {exc}")
            raw = skill_view("linked-assets")

        result = json.loads(raw)
        assert not result["linked_files"]
        assert result["linked_files_summary"]["shown"] == 0

    def test_support_file_read_remains_bound_across_category_symlink_swap(
        self, tmp_path, monkeypatch
    ):
        skills_root = tmp_path / "skills"
        packages = tmp_path / "packages"
        original_category = packages / "original"
        replacement_category = packages / "replacement"
        skills_root.mkdir()
        original_skill = _make_skill(original_category, "bound-support")
        replacement_skill = replacement_category / "bound-support"
        replacement_skill.mkdir(parents=True)
        os.link(
            original_skill / "SKILL.md",
            replacement_skill / "SKILL.md",
        )
        for skill_dir, value in (
            (original_skill, "ORIGINAL SUPPORT"),
            (replacement_skill, "REPLACEMENT SUPPORT"),
        ):
            (skill_dir / "assets").mkdir()
            (skill_dir / "assets" / "value.txt").write_text(
                value, encoding="utf-8"
            )
        category_link = skills_root / "linked"
        category_link.symlink_to(original_category, target_is_directory=True)
        real_path_read_text = Path.read_text
        real_os_open = os.open
        swapped = False

        def swap_package():
            nonlocal swapped
            if not swapped:
                swapped = True
                category_link.unlink()
                category_link.symlink_to(
                    replacement_category, target_is_directory=True
                )

        def swap_before_legacy_read(path, *args, **kwargs):
            if path.name == "value.txt":
                swap_package()
            return real_path_read_text(path, *args, **kwargs)

        def swap_during_bound_open(path, *args, **kwargs):
            if path == "assets" and kwargs.get("dir_fd") is not None:
                swap_package()
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", swap_before_legacy_read)
        monkeypatch.setattr(os, "open", swap_during_bound_open)
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[],
            ),
        ):
            result = json.loads(
                skill_view("bound-support", file_path="assets/value.txt")
            )

        assert swapped is True
        assert result["success"] is True
        assert result["content"] == "ORIGINAL SUPPORT"

    def test_linked_manifest_remains_bound_across_category_symlink_swap(
        self, tmp_path, monkeypatch
    ):
        skills_root = tmp_path / "skills"
        packages = tmp_path / "packages"
        original_category = packages / "original"
        replacement_category = packages / "replacement"
        skills_root.mkdir()
        original_skill = _make_skill(original_category, "bound-manifest")
        replacement_skill = replacement_category / "bound-manifest"
        replacement_skill.mkdir(parents=True)
        os.link(
            original_skill / "SKILL.md",
            replacement_skill / "SKILL.md",
        )
        (original_skill / "assets").mkdir()
        (original_skill / "assets" / "original.txt").write_text(
            "original", encoding="utf-8"
        )
        (replacement_skill / "assets").mkdir()
        (replacement_skill / "assets" / "replacement.txt").write_text(
            "replacement"
        )
        category_link = skills_root / "linked"
        category_link.symlink_to(original_category, target_is_directory=True)
        real_walk = os.walk
        real_scandir = os.scandir
        swapped = False

        def swap_package():
            nonlocal swapped
            if not swapped:
                swapped = True
                category_link.unlink()
                category_link.symlink_to(
                    replacement_category, target_is_directory=True
                )

        def swap_before_legacy_walk(*args, **kwargs):
            if str(args[0]).endswith("/assets"):
                swap_package()
            return real_walk(*args, **kwargs)

        def swap_during_bound_scan(path):
            if isinstance(path, int):
                swap_package()
            return real_scandir(path)

        monkeypatch.setattr(os, "walk", swap_before_legacy_walk)
        monkeypatch.setattr(os, "scandir", swap_during_bound_scan)
        with (
            patch("tools.skills_tool.SKILLS_DIR", skills_root),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[],
            ),
        ):
            result = json.loads(skill_view("bound-manifest"))

        assert swapped is True
        assert result["success"] is True
        assert result["linked_files"]["assets"] == ["assets/original.txt"]

    def test_view_tags_from_metadata(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "tagged",
                frontmatter_extra="metadata:\n  hermes:\n    tags: [fine-tuning, llm]\n",
            )
            raw = skill_view("tagged")
        result = json.loads(raw)
        assert "fine-tuning" in result["tags"]
        assert "llm" in result["tags"]

    def test_view_nonexistent_skills_dir(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path / "nope"):
            raw = skill_view("anything")
        result = json.loads(raw)
        assert result["success"] is False

    def test_view_disabled_skill_blocked(self, tmp_path):
        """Disabled skills should not be viewable via skill_view."""
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._is_skill_disabled", return_value=True),
        ):
            _make_skill(tmp_path, "hidden-skill")
            blocked = json.loads(skill_view("hidden-skill"))
        assert blocked["success"] is False
        assert "disabled" in blocked["error"].lower()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._is_skill_disabled", return_value=False),
        ):
            _make_skill(tmp_path, "active-skill")
            allowed = json.loads(skill_view("active-skill"))
        assert allowed["success"] is True

    def test_view_finds_skill_in_symlinked_category_dir(self, tmp_path):
        external_root = tmp_path / "repo"
        skills_root = tmp_path / "skills"
        skills_root.mkdir()

        external_category = _symlink_category(skills_root, external_root, "linked")
        _make_skill(external_category.parent, "knowledge-brain", category="linked")

        with patch("tools.skills_tool.SKILLS_DIR", skills_root):
            raw = skill_view("knowledge-brain")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["name"] == "knowledge-brain"


class TestSkillViewSecureSetupOnLoad:
    def test_context_secret_callbacks_do_not_cross_concurrent_turns(self, monkeypatch):
        """Two gateway turns retain their own secure-prompt destination."""
        monkeypatch.setattr(skills_tool_module, "_secret_capture_callback", None)
        a_entered = threading.Event()
        b_entered = threading.Event()
        release = threading.Event()
        calls = []

        def run(label, name, entered, other_entered):
            def callback(var_name, _prompt, _metadata=None):
                calls.append((label, var_name))
                entered.set()
                assert other_entered.wait(timeout=2)
                assert release.wait(timeout=2)
                return {
                    "success": True,
                    "stored_as": var_name,
                    "validated": False,
                    "skipped": False,
                }

            token = skills_tool_module.set_secret_capture_callback_context(callback)
            try:
                skills_tool_module._capture_required_environment_variables(
                    label,
                    [{"name": name, "prompt": f"prompt for {label}"}],
                )
            finally:
                skills_tool_module.reset_secret_capture_callback_context(token)

        thread_a = threading.Thread(target=run, args=("A", "A_SECRET", a_entered, b_entered))
        thread_b = threading.Thread(target=run, args=("B", "B_SECRET", b_entered, a_entered))
        thread_a.start()
        thread_b.start()
        assert a_entered.wait(timeout=2)
        assert b_entered.wait(timeout=2)
        release.set()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)

        assert sorted(calls) == [("A", "A_SECRET"), ("B", "B_SECRET")]

    def test_requests_missing_required_env_and_continues(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)
        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append(
                {
                    "var_name": var_name,
                    "prompt": prompt,
                    "metadata": metadata,
                }
            )
            os.environ[var_name] = "stored-in-test"
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": False,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "gif-search",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                    "    help: Get a key from https://developers.google.com/tenor\n"
                    "    required_for: full functionality\n"
                ),
            )
            raw = skill_view("gif-search")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["name"] == "gif-search"
        assert calls == [
            {
                "var_name": "TENOR_API_KEY",
                "prompt": "Tenor API key",
                "metadata": {
                    "skill_name": "gif-search",
                    "help": "Get a key from https://developers.google.com/tenor",
                    "required_for": "full functionality",
                },
            }
        ]
        assert result["required_environment_variables"][0]["name"] == "TENOR_API_KEY"
        assert result["setup_skipped"] is False

    def test_allows_skipping_secure_setup_and_still_loads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)

        def fake_secret_callback(var_name, prompt, metadata=None):
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": True,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "gif-search",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            raw = skill_view("gif-search")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_skipped"] is True
        assert result["content"].startswith("---")

# ---------------------------------------------------------------------------
# skill_matches_platform
# ---------------------------------------------------------------------------


class TestSkillMatchesPlatform:
    """Tests for the platforms frontmatter field filtering."""

    def test_missing_or_empty_platforms_matches_everything(self):
        assert skill_matches_platform({}) is True
        assert skill_matches_platform({"name": "foo"}) is True
        assert skill_matches_platform({"platforms": []}) is True
        assert skill_matches_platform({"platforms": None}) is True


    def test_string_form_case_insensitive_and_unknown_platforms(self):
        with patch("agent.skill_utils.sys") as mock_sys:
            mock_sys.platform = "darwin"
            # A single string value is treated as a one-element list.
            assert skill_matches_platform({"platforms": "macos"}) is True
            assert skill_matches_platform({"platforms": ["MacOS"]}) is True
            assert skill_matches_platform({"platforms": ["MACOS"]}) is True

            mock_sys.platform = "linux"
            assert skill_matches_platform({"platforms": "macos"}) is False
            assert skill_matches_platform({"platforms": ["freebsd"]}) is False


# ---------------------------------------------------------------------------
# _find_all_skills — platform filtering integration
# ---------------------------------------------------------------------------


class TestFindAllSkillsPlatformFiltering:
    """Test that _find_all_skills respects the platforms field."""

    def test_discovery_filters_on_platform(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            _make_skill(tmp_path, "universal-skill")
            _make_skill(tmp_path, "mac-only", frontmatter_extra="platforms: [macos]\n")

            mock_sys.platform = "linux"
            linux = {s["name"] for s in _find_all_skills()}
            mock_sys.platform = "darwin"
            darwin = {s["name"] for s in _find_all_skills()}
            mock_sys.platform = "win32"
            win = {s["name"] for s in _find_all_skills()}

        assert linux == {"universal-skill"}
        assert darwin == {"universal-skill", "mac-only"}
        # Skills without a platforms field appear on every platform.
        assert win == {"universal-skill"}

    def test_multi_platform_skill(self, tmp_path):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            _make_skill(
                tmp_path, "cross-plat", frontmatter_extra="platforms: [macos, linux]\n"
            )
            mock_sys.platform = "darwin"
            skills_darwin = _find_all_skills()
            mock_sys.platform = "linux"
            skills_linux = _find_all_skills()
            mock_sys.platform = "win32"
            skills_win = _find_all_skills()
        assert len(skills_darwin) == 1
        assert len(skills_linux) == 1
        assert len(skills_win) == 0


# ---------------------------------------------------------------------------
# _find_all_skills — env-var prerequisites must not change the listing
# ---------------------------------------------------------------------------


class TestFindAllSkillsSecureSetup:
    def test_listing_shape_independent_of_env_var_prereqs(self, tmp_path, monkeypatch):
        # A remote backend must not be probed just to build the listing.
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.delenv("NONEXISTENT_API_KEY_XYZ", raising=False)
        monkeypatch.setenv("MY_PRESENT_KEY", "val")

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "needs-key",
                frontmatter_extra="prerequisites:\n  env_vars: [NONEXISTENT_API_KEY_XYZ]\n",
            )
            _make_skill(
                tmp_path,
                "has-key",
                frontmatter_extra="prerequisites:\n  env_vars: [MY_PRESENT_KEY]\n",
            )
            _make_skill(tmp_path, "simple-skill")
            skills = _find_all_skills()

        assert {s["name"] for s in skills} == {"needs-key", "has-key", "simple-skill"}
        for skill in skills:
            assert "readiness_status" not in skill
            assert "missing_prerequisites" not in skill


class TestSkillViewPrerequisites:
    def test_legacy_prerequisites_expose_required_env_setup_metadata(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MISSING_KEY_XYZ", raising=False)
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "gated-skill",
                frontmatter_extra="prerequisites:\n  env_vars: [MISSING_KEY_XYZ]\n",
            )
            raw = skill_view("gated-skill")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is True
        assert result["missing_required_environment_variables"] == ["MISSING_KEY_XYZ"]
        assert result["required_environment_variables"] == [
            {
                "name": "MISSING_KEY_XYZ",
                "prompt": "Enter value for MISSING_KEY_XYZ",
            }
        ]


    def test_remote_backend_treats_persisted_env_as_available(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "remote-ready",
                frontmatter_extra="prerequisites:\n  env_vars: [PERSISTED_REMOTE_KEY]\n",
            )
            from hermes_cli.config import save_env_value

            save_env_value("PERSISTED_REMOTE_KEY", "persisted-value")
            monkeypatch.delenv("PERSISTED_REMOTE_KEY", raising=False)
            raw = skill_view("remote-ready")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is False
        assert result["missing_required_environment_variables"] == []
        assert result["readiness_status"] == "available"

    def test_no_setup_metadata_when_no_required_envs(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "plain-skill")
            raw = skill_view("plain-skill")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is False
        assert result["required_environment_variables"] == []

    def test_skill_view_treats_backend_only_env_as_setup_needed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "backend-ready",
                frontmatter_extra="prerequisites:\n  env_vars: [BACKEND_ONLY_KEY]\n",
            )
            raw = skill_view("backend-ready")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is True
        assert result["missing_required_environment_variables"] == ["BACKEND_ONLY_KEY"]

    def test_local_env_missing_keeps_setup_needed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("SHELL_ONLY_KEY", raising=False)

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "shell-ready",
                frontmatter_extra="prerequisites:\n  env_vars: [SHELL_ONLY_KEY]\n",
            )
            raw = skill_view("shell-ready")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is True
        assert result["missing_required_environment_variables"] == ["SHELL_ONLY_KEY"]
        assert result["readiness_status"] == "setup_needed"

    @pytest.mark.parametrize(
        "backend",
        ["ssh", "daytona", "docker", "singularity", "modal", "vercel_sandbox"],
    )
    def test_remote_backend_becomes_available_after_local_secret_capture(
        self, tmp_path, monkeypatch, backend
    ):
        monkeypatch.setenv("TERMINAL_ENV", backend)
        monkeypatch.delenv("TENOR_API_KEY", raising=False)
        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append((var_name, prompt, metadata))
            os.environ[var_name] = "captured-locally"
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": False,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "gif-search",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            raw = skill_view("gif-search")

        result = json.loads(raw)
        assert result["success"] is True
        assert len(calls) == 1
        assert result["setup_needed"] is False
        assert result["readiness_status"] == "available"
        assert result["missing_required_environment_variables"] == []
        assert "setup_note" not in result

    def test_skill_view_surfaces_skill_read_errors(self, tmp_path, monkeypatch):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "broken-skill")
            skill_md = tmp_path / "broken-skill" / "SKILL.md"
            skill_md.write_bytes(b"\xff")
            raw = skill_view("broken-skill")

        result = json.loads(raw)
        assert result["success"] is False
        assert result["error_code"] == "skills_discovery_incomplete"
        assert "partial" in result["error"].lower()
        assert "invalid start byte" in result["detail"]

    def test_legacy_flat_md_skill_preserves_frontmatter_metadata(self, tmp_path):
        flat_skill = tmp_path / "legacy-skill.md"
        flat_skill.write_text(
            """\
---
name: legacy-flat
description: Legacy flat skill.
metadata:
  hermes:
    tags: [legacy, flat]
required_environment_variables:
  - name: LEGACY_KEY
    prompt: Legacy key
---

# Legacy Flat

Do the legacy thing.
""",
            encoding="utf-8",
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view("legacy-skill")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["name"] == "legacy-flat"
        assert result["description"] == "Legacy flat skill."
        assert result["tags"] == ["legacy", "flat"]
        assert result["required_environment_variables"] == [
            {"name": "LEGACY_KEY", "prompt": "Legacy key"}
        ]

    def test_successful_secret_capture_reloads_empty_env_placeholder(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("TENOR_API_KEY", raising=False)

        def fake_secret_callback(var_name, prompt, metadata=None):
            from hermes_cli.config import save_env_value

            save_env_value(var_name, "captured-value")
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": False,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(
                tmp_path,
                "gif-search",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            from hermes_cli.config import save_env_value

            save_env_value("TENOR_API_KEY", "")
            raw = skill_view("gif-search")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["setup_needed"] is False
        assert result["missing_required_environment_variables"] == []
        assert result["readiness_status"] == "available"


class TestSkillViewCollisionDetection:
    """Regression tests for skill_view name collision handling.

    When a skill name resolves to multiple paths across the local skills
    dir and external_dirs, skill_view must refuse to guess. Silent
    shadowing — where ``/skills`` shows the local version but
    ``skill_view`` loads the external one — is the bug class this guards
    against. Reproduces with `skills.external_dirs` registered in
    config.yaml and a same-name skill nested under a category locally.

    Adapted from a regression suite originally proposed by @polkn in PR
    #6136 (which used local-first precedence). The collision-refusal
    behavior preserves the same protection without silently picking a
    side, and gives the user an actionable hint (use the categorized
    path) to recover.
    """

    def _patch_dirs(self, local_dir, external_dirs):
        """Patch SKILLS_DIR (module-level) and get_external_skills_dirs at source."""
        return (
            patch("tools.skills_tool.SKILLS_DIR", local_dir),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=list(external_dirs),
            ),
        )

    def test_nested_local_collides_with_top_level_external(self, tmp_path):
        """The original bug scenario: nested local + top-level external,
        same name. Now refuses with both paths surfaced."""
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()

        _make_skill(
            local_dir,
            "explore-codebase",
            category="foundations/runtime",
            body="LOCAL VERSION",
        )
        _make_skill(external_dir, "explore-codebase", body="EXTERNAL VERSION")

        p1, p2 = self._patch_dirs(local_dir, [external_dir])
        with p1, p2:
            raw = skill_view("explore-codebase")

        result = json.loads(raw)
        assert result["success"] is False
        assert "Ambiguous skill name 'explore-codebase'" in result["error"]
        assert "matches" in result
        assert len(result["matches"]) == 2
        # Both paths surfaced
        assert any("foundations/runtime" in p for p in result["matches"])
        assert any("external" in p for p in result["matches"])
        assert "hint" in result


    def test_support_markdown_does_not_collide_with_real_skill(self, tmp_path):
        """Supporting reference docs named <skill>.md are not skills.

        A real-world regression had creative/sketch/SKILL.md become
        unloadable because another skill carried
        references/styles/sketch.md. Support files are loaded via
        skill_view(skill, file_path=...), not as bare skill names.
        """
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()

        _make_skill(local_dir, "article-illustrator", category="creative")
        support_file = (
            local_dir
            / "creative"
            / "article-illustrator"
            / "references"
            / "styles"
            / "sketch.md"
        )
        support_file.parent.mkdir(parents=True, exist_ok=True)
        support_file.write_text(
            "# Sketch style support doc\n", encoding="utf-8"
        )
        _make_skill(local_dir, "sketch", category="creative", body="REAL SKETCH SKILL")

        p1, p2 = self._patch_dirs(local_dir, [external_dir])
        with p1, p2:
            raw = skill_view("sketch")

        result = json.loads(raw)
        assert result["success"] is True
        assert result["path"] == "creative/sketch/SKILL.md"
        assert "REAL SKETCH SKILL" in result["content"]


    def test_two_externals_same_name_also_refuse(self, tmp_path):
        """Collision detection is symmetric — two external dirs with
        same-name skills also trigger the refusal."""
        local_dir = tmp_path / "local"
        ext_a = tmp_path / "ext_a"
        ext_b = tmp_path / "ext_b"
        local_dir.mkdir()
        ext_a.mkdir()
        ext_b.mkdir()

        _make_skill(ext_a, "pr", body="EXT_A VERSION")
        _make_skill(ext_b, "pr", body="EXT_B VERSION")

        p1, p2 = self._patch_dirs(local_dir, [ext_a, ext_b])
        with p1, p2:
            raw = skill_view("pr")

        result = json.loads(raw)
        assert result["success"] is False
        assert "Ambiguous" in result["error"]
        assert len(result["matches"]) == 2

    def test_local_only_skill_loads_normally(self, tmp_path):
        """Sanity: a single local skill (no external collision) loads
        without any error."""
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()

        _make_skill(
            local_dir,
            "my-skill",
            category="foundations/runtime",
            body="LOCAL BODY",
        )

        p1, p2 = self._patch_dirs(local_dir, [external_dir])
        with p1, p2:
            raw = skill_view("my-skill")

        result = json.loads(raw)
        assert result["success"] is True
        assert "LOCAL BODY" in result["content"]


class TestSkillViewIncompleteExternalDiscovery:
    """Accessible external roots must still be complete before local loading."""

    def _patch_dirs(self, local_dir, external_dir):
        return (
            patch("tools.skills_tool.SKILLS_DIR", local_dir),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[external_dir],
            ),
        )

    @staticmethod
    def _assert_incomplete(raw):
        result = json.loads(raw)
        assert result["success"] is False
        assert result["error_code"] == "skills_discovery_incomplete"
        assert "partial" in result["error"].lower()
        return result

    def test_corrupt_external_skill_frontmatter_refuses_local_candidate(self, tmp_path):
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()
        _make_skill(local_dir, "local-only", body="LOCAL BODY")
        corrupt = _make_skill(external_dir, "broken-external") / "SKILL.md"
        corrupt.write_text("---\nname: [unterminated\n---\n", encoding="utf-8")
        p1, p2 = self._patch_dirs(local_dir, external_dir)
        with p1, p2:
            result = self._assert_incomplete(skill_view("local-only"))

        assert str(corrupt) in result["detail"]

    @pytest.mark.parametrize(
        "broken_frontmatter",
        (
            "---\nname: hidden-collision\n# missing closing fence\n",
            "---\n- name: hidden-collision\n---\n\nBody.\n",
        ),
        ids=("unclosed-fence", "non-mapping-top-level"),
    )
    def test_structurally_invalid_external_frontmatter_refuses_local_candidate(
        self, tmp_path, broken_frontmatter
    ):
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()
        _make_skill(local_dir, "local-only", body="LOCAL BODY")
        broken = _make_skill(external_dir, "broken-external") / "SKILL.md"
        broken.write_text(broken_frontmatter, encoding="utf-8")

        p1, p2 = self._patch_dirs(local_dir, external_dir)
        with p1, p2:
            result = self._assert_incomplete(skill_view("local-only"))

        assert str(broken) in result["detail"]

    def test_invalid_unrelated_markdown_and_support_package_do_not_block(
        self, tmp_path
    ):
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()
        _make_skill(local_dir, "local-only", body="LOCAL BODY")
        unrelated = external_dir / "notes" / "unrelated.md"
        unrelated.parent.mkdir()
        unrelated.write_text("---\n- not-a-skill\n---\n", encoding="utf-8")
        umbrella = _make_skill(external_dir, "umbrella")
        support_package = umbrella / "references" / "preserved" / "SKILL.md"
        support_package.parent.mkdir(parents=True)
        support_package.write_text(
            "---\nname: never-closed\n", encoding="utf-8"
        )

        p1, p2 = self._patch_dirs(local_dir, external_dir)
        with p1, p2:
            result = json.loads(skill_view("local-only"))

        assert result["success"] is True
        assert "LOCAL BODY" in result["content"]

    def test_symlinked_skill_root_swap_cannot_change_discovered_canonical_file(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "local"
        linked_parent = tmp_path / "linked-packages"
        original_category = linked_parent / "original"
        replacement_category = linked_parent / "replacement"
        local_dir.mkdir()
        original_category.mkdir(parents=True)
        replacement_category.mkdir(parents=True)
        original_skill = _make_skill(
            original_category, "linked-skill", body="ORIGINAL BODY"
        )
        replacement_skill = replacement_category / "linked-skill"
        replacement_skill.mkdir()
        try:
            os.link(
                original_skill / "SKILL.md",
                replacement_skill / "SKILL.md",
            )
        except OSError as exc:
            pytest.skip(f"hard links unavailable in test environment: {exc}")
        (replacement_skill / "assets").mkdir()
        (replacement_skill / "assets" / "swapped.txt").write_text(
            "SWAPPED PACKAGE", encoding="utf-8"
        )
        category_link = local_dir / "linked"
        try:
            category_link.symlink_to(original_category, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable in test environment: {exc}")

        real_parse = skills_tool_module._parse_frontmatter
        swapped = False

        def swap_after_discovery(content):
            nonlocal swapped
            result = real_parse(content)
            if not swapped and "ORIGINAL BODY" in content:
                swapped = True
                category_link.unlink()
                category_link.symlink_to(
                    replacement_category, target_is_directory=True
                )
            return result

        monkeypatch.setattr(
            skills_tool_module, "_parse_frontmatter", swap_after_discovery
        )
        with (
            patch("tools.skills_tool.SKILLS_DIR", local_dir),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[],
            ),
        ):
            result = self._assert_incomplete(skill_view("linked-skill"))

        assert swapped is True
        assert "changed during discovery" in result["detail"]

    def test_unreadable_external_skill_refuses_local_candidate(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()
        _make_skill(local_dir, "local-only", body="LOCAL BODY")
        unreadable = _make_skill(external_dir, "unreadable-external") / "SKILL.md"
        real_open = os.open

        def reject_external_open(path, *args, **kwargs):
            if Path(path) == unreadable:
                raise PermissionError("external SKILL.md is unreadable")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", reject_external_open)
        p1, p2 = self._patch_dirs(local_dir, external_dir)
        with p1, p2:
            result = self._assert_incomplete(skill_view("local-only"))

        assert "unreadable" in result["detail"]

    def test_external_subtree_traversal_error_refuses_local_candidate(
        self, tmp_path, monkeypatch
    ):
        local_dir = tmp_path / "local"
        external_dir = tmp_path / "external"
        local_dir.mkdir()
        external_dir.mkdir()
        _make_skill(local_dir, "local-only", body="LOCAL BODY")

        from agent.skill_utils import iter_skill_index_files as real_iter_skill_index_files

        def traversal_error_iterator(root, filename, *, on_error=None):
            if root == external_dir and on_error is not None:
                on_error(PermissionError("cannot traverse external subtree"))
            yield from real_iter_skill_index_files(root, filename, on_error=on_error)

        monkeypatch.setattr(
            "agent.skill_utils.iter_skill_index_files", traversal_error_iterator
        )
        p1, p2 = self._patch_dirs(local_dir, external_dir)
        with p1, p2:
            result = self._assert_incomplete(skill_view("local-only"))

        assert "cannot traverse external subtree" in result["detail"]
