"""Contract tests for the bundled test-driven-development skill."""

import re
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = (
    REPO_ROOT
    / "skills"
    / "software-development"
    / "test-driven-development"
    / "SKILL.md"
)

REQUIRED_SECTIONS = [
    "## When to Use",
    "## The Iron Law",
    "## Red-Green-Refactor Cycle",
    "## Avoid Horizontal Slices",
    "## Why Order Matters",
    "## Common Rationalizations",
    "## Red Flags — STOP and Start Over",
    "## Verification Checklist",
    "## When Stuck",
    "## Hermes Agent Integration",
    "## Testing Anti-Patterns",
]

CYCLE_STEPS = [
    "### RED — Write Failing Test",
    "### Verify RED — Watch It Fail",
    "### GREEN — Minimal Code",
    "### Verify GREEN — Watch It Pass",
    "### REFACTOR — Clean Up",
]


def _frontmatter_and_body():
    content = SKILL_MD.read_text(encoding="utf-8")
    assert content.startswith("---")
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "frontmatter must close with ---"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.end() + 3 :]
    return fm, body


class TestTDDSkillContract:
    """Test that SKILL.md adheres to the project hardline standards."""

    def test_skill_file_exists(self):
        assert SKILL_MD.is_file()

    def test_frontmatter_required_fields(self):
        fm, _ = _frontmatter_and_body()
        for field in ("name", "description", "version", "author", "license", "platforms"):
            assert field in fm, f"missing frontmatter field: {field}"
        assert fm["name"] == "test-driven-development"
        hermes = fm["metadata"]["hermes"]
        assert hermes["tags"]
        assert "related_skills" in hermes

    def test_description_hardline(self):
        fm, _ = _frontmatter_and_body()
        desc = fm["description"]
        assert len(desc) <= 60, f"description is {len(desc)} chars; max allowed is 60"
        assert desc.endswith("."), "description must end with a period"
        assert not re.search(
            r"\b(powerful|comprehensive|seamless|revolutionary|cutting-edge|state-of-the-art)\b",
            desc,
            re.I,
        )

    def test_related_skills_resolve_in_repo(self):
        fm, _ = _frontmatter_and_body()
        for name in fm["metadata"]["hermes"]["related_skills"]:
            hits = list(REPO_ROOT.glob(f"skills/**/{name}/SKILL.md")) + list(
                REPO_ROOT.glob(f"optional-skills/**/{name}/SKILL.md")
            )
            assert hits, f"related_skills entry does not resolve in repo: {name}"

    def test_no_machine_local_paths(self):
        content = SKILL_MD.read_text(encoding="utf-8")
        assert "/home/" not in content
        assert not re.search(r"[A-Z]:\\\\Users", content)

    def test_required_sections_present(self):
        _, body = _frontmatter_and_body()
        for section in REQUIRED_SECTIONS:
            assert section in body, f"missing section: {section}"

    def test_red_green_refactor_cycle_sequence(self):
        _, body = _frontmatter_and_body()
        positions = [body.index(step) for step in CYCLE_STEPS]
        assert positions == sorted(positions), "cycle steps must be in RED -> Verify RED -> GREEN -> Verify GREEN -> REFACTOR order"

    def test_iron_law_and_tracer_bullet_discipline(self):
        _, body = _frontmatter_and_body()
        assert "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST" in body
        assert "tracer bullets" in body.lower()
        assert "terminal" in body
        assert "delegate_task" in body
