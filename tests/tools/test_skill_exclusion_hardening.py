"""Tests for hard-exclusion of hidden skill storage dirs (`.library`, `.archive`).

Regression coverage for the discovery-exclusion hardening surfaced by issue
#35800: dot-prefixed cold-storage dirs must be invisible to skill discovery,
name resolution, and legacy rglob scans — and `skill_view` must not read a
parked/archived skill by name.

Explicit-path reads (`skill_view` with the full relative path inside an
excluded dir) remain allowed: previewing a parked skill's SKILL.md before
deciding to restore it is a legitimate workflow (`hermes curator restore`).
"""

import json
from unittest.mock import patch

import pytest

from agent.skill_utils import is_excluded_skill_path, iter_skill_index_files
from tools.skills_tool import skill_view


@pytest.fixture()
def fake_skills(tmp_path):
    """Skills dir with an active skill, an archived skill, and a library skill."""
    skills_dir = tmp_path / "skills"
    active = skills_dir / "active-skill"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text("---\nname: active-skill\n---\n# Active\n")

    archived = skills_dir / ".archive" / "2026-08" / "archived-skill"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text("---\nname: archived-skill\n---\n# Archived\n")

    library = skills_dir / ".library" / "creative" / "parked-skill"
    library.mkdir(parents=True)
    (library / "SKILL.md").write_text("---\nname: parked-skill\n---\n# Parked\n")

    # A legacy flat file parked in the library too.
    (skills_dir / ".library" / "legacy-note.md").write_text("# legacy note\n")

    with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
        yield {
            "skills_dir": skills_dir,
            "active": active,
            "archived": archived,
            "library": library,
        }


class TestExclusionMembership:
    def test_library_is_excluded_dir(self):
        from agent.skill_utils import EXCLUDED_SKILL_DIRS

        assert ".library" in EXCLUDED_SKILL_DIRS

    def test_is_excluded_skill_path_flags_library_and_archive(self, fake_skills):
        sd = fake_skills["skills_dir"]
        assert is_excluded_skill_path(sd / ".library" / "creative" / "parked-skill" / "SKILL.md") is True
        assert is_excluded_skill_path(sd / ".archive" / "archived-skill" / "SKILL.md") is True
        assert is_excluded_skill_path(sd / "active-skill" / "SKILL.md") is False


class TestIndexInvisibility:
    def test_iter_skill_index_files_skips_hidden_storage(self, fake_skills):
        found = [p.parent.name for p in iter_skill_index_files(fake_skills["skills_dir"], "SKILL.md")]
        assert "active-skill" in found
        assert "archived-skill" not in found
        assert "parked-skill" not in found


class TestSkillViewExcludedByName:
    def test_archived_skill_not_resolved_by_bare_name(self, fake_skills):
        result = json.loads(skill_view("archived-skill"))
        assert result["success"] is False

    def test_parked_library_skill_not_resolved_by_bare_name(self, fake_skills):
        result = json.loads(skill_view("parked-skill"))
        assert result["success"] is False

    def test_flat_md_in_library_not_resolved_by_name(self, fake_skills):
        result = json.loads(skill_view("legacy-note"))
        assert result["success"] is False

    def test_active_skill_still_resolves(self, fake_skills):
        result = json.loads(skill_view("active-skill"))
        assert result["success"] is True

    def test_explicit_relative_path_still_previews(self, fake_skills):
        """Explicit-path reads stay allowed: preview-before-restore workflow."""
        result = json.loads(skill_view(".archive/2026-08/archived-skill"))
        assert result["success"] is True
        assert "Archived" in result.get("content", "")

    def test_root_excluded_dir_names_not_resolved(self, fake_skills):
        """The excluded dirs themselves are not addressable as bare names."""
        for bare in (".archive", ".library"):
            result = json.loads(skill_view(bare))
            assert result["success"] is False, bare

    def test_skill_view_file_path_restricted_inside_excluded_dir(self, fake_skills):
        """file_path within an explicitly-addressed excluded skill stays inside it."""
        result = json.loads(
            skill_view(".archive/2026-08/archived-skill", file_path="../.env")
        )
        assert result["success"] is False

    def test_symlink_into_excluded_dir_not_indexed(self, fake_skills, tmp_path):
        """A symlink whose target lives inside an excluded dir is not indexed.

        os.walk(followlinks=True) yields the symlink's lexical path, which does
        not contain the excluded dir name. iter_skill_index_files now checks
        the resolved root to close this leak.
        """
        link = tmp_path / "skills" / "linked-archived"
        link.symlink_to(fake_skills["archived"])
        found = [p.parent.name for p in iter_skill_index_files(tmp_path / "skills", "SKILL.md")]
        assert "linked-archived" not in found

    def test_excluded_name_shadowing_active_skill(self, fake_skills):
        """An excluded skill with the same name as an active skill does not shadow it."""
        # Create a same-named skill inside .library
        shadow = fake_skills["skills_dir"] / ".library" / "shadow" / "active-skill"
        shadow.mkdir(parents=True)
        (shadow / "SKILL.md").write_text("---\nname: active-skill\n---\n# Shadow\n")
        result = json.loads(skill_view("active-skill"))
        assert result["success"] is True
        assert "Active" in result.get("content", "")


class TestLegacyScanGuards:
    def test_learning_graph_ignores_hidden_storage(self, fake_skills, monkeypatch):
        """agent.learning_graph._iter_skill_files must not yield excluded paths."""
        from agent import learning_graph

        roots = [("local", fake_skills["skills_dir"])]
        yielded = [path.name for _, path in learning_graph._iter_skill_files(roots)]
        # Only the active skill's SKILL.md is yielded (excluded dirs pruned).
        assert yielded.count("SKILL.md") == 1

    def test_blueprints_lookup_ignores_hidden_storage(self, fake_skills):
        """tools.blueprints skill lookup must not match inside excluded dirs."""
        from tools import blueprints

        spec = blueprints.blueprint_spec_for_installed("archived-skill")
        assert spec is None
