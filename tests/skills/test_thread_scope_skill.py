"""Metadata checks for skills/software-development/thread-scope/SKILL.md.

Mirrors the authoring-standards verification snippet in AGENTS.md
("Skill authoring standards (HARDLINE)").
"""
import re
from pathlib import Path

import yaml

SKILL_PATH = Path("skills/software-development/thread-scope/SKILL.md")


def _frontmatter():
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert match, "SKILL.md must start with a --- frontmatter block"
    return yaml.safe_load(match.group(1)), match.group(2)


class TestFrontmatter:
    def test_description_at_most_60_chars(self):
        text = SKILL_PATH.read_text(encoding="utf-8")
        m = re.search(r'^description: (.*)$', text, re.MULTILINE)
        assert m is not None
        assert len(m.group(1)) <= 60, len(m.group(1))

    def test_description_does_not_repeat_skill_name(self):
        frontmatter, _ = _frontmatter()
        assert frontmatter["name"] not in frontmatter["description"]

    def test_required_fields_present(self):
        frontmatter, _ = _frontmatter()
        for field in ("name", "description", "version", "author", "license", "platforms"):
            assert field in frontmatter, field

    def test_name_matches_directory(self):
        frontmatter, _ = _frontmatter()
        assert frontmatter["name"] == SKILL_PATH.parent.name


class TestSkillBody:
    def test_section_order(self):
        _, body = _frontmatter()
        required_sections = [
            "## When to Use",
            "## Prerequisites",
            "## How to Run",
            "## Quick Reference",
            "## Procedure",
            "## Pitfalls",
            "## Verification",
        ]
        positions = [body.index(s) for s in required_sections]
        assert positions == sorted(positions)

    def test_references_only_real_commands_not_shell_utilities(self):
        _, body = _frontmatter()
        for banned in ("`grep `", "`cat `", "`sed `", "`awk `"):
            assert banned not in body

    def test_references_hermes_scope_cli(self):
        _, body = _frontmatter()
        assert "hermes scope status" in body
        assert "hermes scope create" in body
