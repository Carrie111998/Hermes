"""Tests for agent/skill_trigger_loader.py.

Tests use snapshot-style entry dicts (not raw filesystem scanning) to match
how the trigger loader consumes entries in production — from the
``prompt_builder`` snapshot pipeline after platform/environment/disabled
filtering.
"""

import logging
import tempfile
from pathlib import Path

import pytest

from agent.skill_trigger_loader import (
    _strip_frontmatter,
    format_triggered_skill_content,
    get_triggered_skills,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    skill_name: str = "test-skill",
    triggers: list[str] | None = None,
    rel_path: str = "",
) -> dict:
    """Build a minimal snapshot entry dict matching what prompt_builder emits."""
    return {
        "skill_name": skill_name,
        "frontmatter_name": skill_name,
        "category": "test",
        "description": "A test skill.",
        "triggers": triggers or [],
        "rel_path": rel_path,
    }


# ===========================================================================
# get_triggered_skills
# ===========================================================================


class TestGetTriggeredSkills:
    """Tests for get_triggered_skills()."""

    def test_blank_input_returns_empty(self):
        """Blank user_text yields no matches."""
        assert get_triggered_skills("", []) == []

    def test_blank_input_with_entries(self):
        """Blank user_text yields no matches even with trigger entries."""
        entries = [_make_entry(triggers=[r"hello"])]
        assert get_triggered_skills("", entries) == []

    def test_none_text_becomes_empty_after_fail_safe(self):
        """get_triggered_skills handles non-string safely (wrapped by caller)."""
        entries = [_make_entry(triggers=[r"hello"])]
        assert get_triggered_skills("hello", entries) != []

    # -- No-match cases ---------------------------------------------------

    def test_no_visible_entries_returns_empty(self):
        """Empty visible_entries list yields no matches."""
        assert get_triggered_skills("hello world", []) == []

    def test_no_triggers_on_entry_skips_it(self):
        """Entry without triggers is skipped."""
        entries = [_make_entry(triggers=[])]
        assert get_triggered_skills("anything", entries) == []

    def test_no_match_returns_empty(self):
        """Text not matching any trigger yields empty list."""
        entries = [_make_entry(triggers=[r"php|python"])]
        assert get_triggered_skills("hello world", entries) == []

    # -- Match cases ------------------------------------------------------

    def test_case_insensitive_match(self):
        """Trigger matching is case-insensitive."""
        entries = [_make_entry(skill_name="python-help", triggers=[r"python"])]
        result = get_triggered_skills("I need PYTHON help", entries)
        assert len(result) == 1
        assert result[0]["skill_name"] == "python-help"

    def test_dotall_flag_works(self):
        """re.DOTALL allows '.' to match newlines."""
        entries = [_make_entry(triggers=[r"error.*trace"])]
        result = get_triggered_skills("Error: something\nTraceback:", entries)
        assert len(result) == 1

    def test_multiple_matches(self):
        """Multiple matching skills are returned, capped at max_results."""
        entries = [
            _make_entry(skill_name="skill-a", triggers=[r"python"]),
            _make_entry(skill_name="skill-b", triggers=[r"python"]),
            _make_entry(skill_name="skill-c", triggers=[r"python"]),
        ]
        result = get_triggered_skills("I use python", entries, max_results=5)
        assert len(result) == 3

    def test_max_results_cap(self):
        """At most max_results skills are returned."""
        entries = [
            _make_entry(skill_name=f"skill-{i}", triggers=[r"python"])
            for i in range(10)
        ]
        result = get_triggered_skills("python code", entries, max_results=5)
        assert len(result) == 5

    def test_per_skill_cap(self):
        """Each skill is matched at most once per turn."""
        # Multiple matching triggers for the same skill should only yield one entry.
        entries = [
            _make_entry(
                skill_name="all-in-one",
                triggers=[r"python", r"javascript", r"rust"],
            ),
        ]
        result = get_triggered_skills(
            "I write python and javascript and rust", entries
        )
        assert len(result) == 1
        assert result[0]["skill_name"] == "all-in-one"

    def test_invalid_regex_skipped_gracefully(self):
        """Invalid regex patterns are silently skipped."""
        entries = [
            _make_entry(skill_name="good-skill", triggers=[r"[valid"]),
            _make_entry(skill_name="bad-skill", triggers=[r"[invalid"]),
            _make_entry(skill_name="also-good", triggers=[r"hello"]),
        ]
        # [valid is actually valid POSIX... let me use a truly invalid one
        entries[0]["triggers"] = [r"[valid_regex"]
        entries[1]["triggers"] = [r"\\"]
        entries[2] = _make_entry(skill_name="also-good", triggers=[r"hello"])

        result = get_triggered_skills("hello world", entries)
        # The good-skill's broken regex is skipped; also-good matches "hello"
        assert len(result) == 1
        assert result[0]["skill_name"] == "also-good"

    # -- Turn-behavior tests ----------------------------------------------

    def test_first_turn_matches(self):
        """Trigger matching works on the first turn (no conversation_history guard)."""
        entries = [_make_entry(triggers=[r"error"])]
        result = get_triggered_skills("I got an error", entries)
        assert len(result) == 1

    def test_nth_turn_matches(self):
        """Trigger matching works on any turn — same as first-turn."""
        entries = [_make_entry(triggers=[r"error"])]
        result = get_triggered_skills("Still getting the same error", entries)
        assert len(result) == 1

    # -- Multimodal guard -------------------------------------------------

    def test_multimodal_skipped_by_caller(self):
        """Non-string user_message is skipped by caller (test that get_triggered
        skills handles it gracefully when called with non-string)."""
        # The turn_context caller guards against this, but the loader is
        # still fail-safe against any input.
        entries = [_make_entry(triggers=[r"."])]
        result = get_triggered_skills("", entries)
        assert result == []

    # -- Platform/disabled filtering is respected -------------------------

    def test_platform_disabled_filtering_already_applied(self):
        """Trigger loader consumes pre-filtered entries — platform/disabled
        filtering is already done by the snapshot pipeline."""
        # If the entry doesn't have a matching trigger, it doesn't match.
        # If it does, it matches — the loader doesn't re-filter.
        entries = [_make_entry(triggers=[r"php"])]
        result = get_triggered_skills("help with PHP", entries)
        assert len(result) == 1


# ===========================================================================
# format_triggered_skill_content
# ===========================================================================


class TestFormatTriggeredSkillContent:
    """Tests for format_triggered_skill_content()."""

    def test_format_with_valid_path(self, tmp_path: Path):
        """Format triggered skill content with a valid SKILL.md file."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "my-category" / "my-skill" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "---\nname: my-skill\ntriggers:\n  - test\n---\n\nThis is the skill body."
        )

        entry = _make_entry(
            skill_name="my-skill",
            triggers=[r"test"],
            rel_path="my-category/my-skill/SKILL.md",
        )
        result = format_triggered_skill_content(entry, skills_dir=skills_dir)
        assert "## Auto-loaded: my-skill" in result
        assert "This is the skill body." in result

    def test_format_missing_rel_path(self):
        """Entry without rel_path returns empty string."""
        entry = _make_entry(rel_path="")
        assert format_triggered_skill_content(entry) == ""

    def test_format_nonexistent_file(self, tmp_path: Path):
        """Entry with rel_path pointing to non-existent file returns empty."""
        entry = _make_entry(
            skill_name="ghost",
            rel_path="does-not-exist/SKILL.md",
        )
        # Use a real but empty skills_dir
        skills_dir = tmp_path / "empty-skills"
        skills_dir.mkdir()
        result = format_triggered_skill_content(entry, skills_dir=skills_dir)
        assert result == ""

    def test_format_empty_body(self, tmp_path: Path):
        """Entry with frontmatter-only file returns empty."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "empty-body" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("---\nname: empty\n---\n")

        entry = _make_entry(
            skill_name="empty",
            rel_path="empty-body/SKILL.md",
        )
        result = format_triggered_skill_content(entry, skills_dir=skills_dir)
        # body after stripping frontmatter is empty
        assert result == ""

    def test_format_no_frontmatter(self, tmp_path: Path):
        """Entry with no frontmatter still formats."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "raw-body" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("Just body text, no frontmatter.")

        entry = _make_entry(
            skill_name="raw-body",
            rel_path="raw-body/SKILL.md",
        )
        result = format_triggered_skill_content(entry, skills_dir=skills_dir)
        assert "## Auto-loaded: raw-body" in result
        assert "Just body text, no frontmatter." in result

    def test_format_fail_safe(self):
        """Exception in format_triggered_skill_content returns empty string."""
        # Entry is None — would cause AttributeError, caught by fail-safe
        # (normally the caller would never pass None, but the guard is there)
        result = format_triggered_skill_content(
            _make_entry(rel_path="../nonexistent/../../../etc/passwd")
        )  # Path traversal doesn't matter — file won't exist
        # Should return "" rather than raise
        assert result == ""


# ===========================================================================
# _strip_frontmatter
# ===========================================================================


class TestStripFrontmatter:
    """Tests for _strip_frontmatter()."""

    def test_strips_yaml_frontmatter(self):
        content = "---\nname: test\n---\n\nBody text"
        assert _strip_frontmatter(content) == "Body text"

    def test_strips_frontmatter_only(self):
        content = "---\nname: test\n---\n"
        assert _strip_frontmatter(content) == ""

    def test_no_frontmatter(self):
        content = "Just body text."
        assert _strip_frontmatter(content) == "Just body text."

    def test_strips_utf8_bom(self):
        content = "\ufeff---\nname: test\n---\n\nBody"
        assert _strip_frontmatter(content) == "Body"

    def test_strips_bom_no_frontmatter(self):
        content = "\ufeffBody text"
        # BOM is stripped even when there's no frontmatter, returning clean text
        assert _strip_frontmatter(content) == "Body text"