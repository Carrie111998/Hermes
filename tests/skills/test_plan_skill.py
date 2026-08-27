"""Contract tests for the bundled plan skill."""

import re
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = (
    REPO_ROOT
    / "skills"
    / "software-development"
    / "plan"
    / "SKILL.md"
)

REQUIRED_SECTIONS = [
    "## Core behavior",
    "## Output requirements",
    "## Save location",
    "## Interaction style",
    "## Overview",
    "## When a Full Implementation Plan Helps",
    "## Bite-Sized Task Granularity",
    "## Plan Document Structure",
    "## Writing Process",
    "## Principles",
    "## Common Mistakes",
    "## Execution Handoff",
    "## Remember",
]

WRITING_PROCESS_STEPS = [
    "### Step 1: Understand Requirements",
    "### Step 2: Explore the Codebase",
    "### Step 3: Design Approach",
    "### Step 4: Write Tasks",
    "### Step 5: Add Complete Details",
    "### Step 6: Review the Plan",
]


def _frontmatter_and_body():
    content = SKILL_MD.read_text(encoding="utf-8")
    assert content.startswith("---")
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "frontmatter must close with ---"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.end() + 3 :]
    return fm, body


class TestPlanSkillContract:
    """Test that SKILL.md adheres to the project hardline standards."""

    def test_skill_file_exists(self):
        assert SKILL_MD.is_file()

    def test_frontmatter_required_fields(self):
        fm, _ = _frontmatter_and_body()
        for field in ("name", "description", "version", "author", "license", "platforms"):
            assert field in fm, f"missing frontmatter field: {field}"
        assert fm["name"] == "plan"
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

    def test_plan_save_location_and_task_granularity(self):
        _, body = _frontmatter_and_body()
        assert ".hermes/plans/" in body
        assert "2-5 minutes" in body
        assert "write_file" in body

    def test_writing_process_sequence(self):
        _, body = _frontmatter_and_body()
        positions = [body.index(step) for step in WRITING_PROCESS_STEPS]
        assert positions == sorted(positions), "writing process steps must be sequential 1 through 6"

    def test_core_disciplines_and_tools(self):
        _, body = _frontmatter_and_body()
        assert "DRY" in body
        assert "YAGNI" in body
        assert "TDD" in body
        assert "subagent-driven-development" in body
        assert "delegate_task" in body
