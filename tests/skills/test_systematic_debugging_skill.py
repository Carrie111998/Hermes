"""Contract tests for the bundled systematic-debugging skill."""

import re
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = (
    REPO_ROOT
    / "skills"
    / "software-development"
    / "systematic-debugging"
    / "SKILL.md"
)

REQUIRED_PHASES = [
    "## Phase 1: Root Cause Investigation",
    "## Phase 2: Pattern Analysis",
    "## Phase 3: Hypothesis and Testing",
    "## Phase 4: Implementation",
]

NATIVE_TOOLS = [
    "search_files",
    "read_file",
    "terminal",
    "web_search",
    "web_extract",
]


def _frontmatter_and_body():
    content = SKILL_MD.read_text(encoding="utf-8")
    assert content.startswith("---")
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "frontmatter must close with ---"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.end() + 3 :]
    return fm, body


class TestSystematicDebuggingSkillContract:
    """Test that SKILL.md adheres to the project hardline standards."""

    def test_skill_file_exists(self):
        assert SKILL_MD.is_file()

    def test_frontmatter_required_fields(self):
        fm, _ = _frontmatter_and_body()
        for field in ("name", "description", "version", "author", "license", "platforms"):
            assert field in fm, f"missing frontmatter field: {field}"
        assert fm["name"] == "systematic-debugging"
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

    def test_four_phases_present_and_ordered(self):
        _, body = _frontmatter_and_body()
        positions = [body.index(phase) for phase in REQUIRED_PHASES]
        assert positions == sorted(positions), "phases must follow 1 -> 2 -> 3 -> 4 sequence"

    def test_iron_law_and_disciplines_present(self):
        _, body = _frontmatter_and_body()
        assert "NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST" in body
        assert "Feedback Loop" in body
        assert "Rule of Three" in body or "Rule of three" in body.lower()
        assert "Question Architecture" in body or "question fundamentals" in body.lower()
        assert "delegate_task" in body

    @pytest.mark.parametrize("tool_name", NATIVE_TOOLS)
    def test_documents_native_hermes_tools(self, tool_name):
        _, body = _frontmatter_and_body()
        assert f"`{tool_name}`" in body, f"native tool {tool_name} must be documented"
