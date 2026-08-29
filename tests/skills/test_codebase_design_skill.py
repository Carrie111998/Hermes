"""Contract tests for the bundled codebase-design skill."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT / "skills" / "software-development" / "codebase-design" / "SKILL.md"
)
REQUIRED_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    assert match, f"missing frontmatter field: {key}"
    return match.group(1).strip().strip('"')


def test_frontmatter_meets_hardline_contribution_standard() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert _frontmatter_value(text, "name") == "codebase-design"
    assert _frontmatter_value(text, "version") == "0.1.0"
    assert _frontmatter_value(text, "license") == "MIT"
    assert _frontmatter_value(text, "author") == (
        "Lucas Veber (vegapunkpa-hue), Hermes Agent"
    )
    assert _frontmatter_value(text, "platforms") == "[linux, macos, windows]"

    description = _frontmatter_value(text, "description")
    assert len(description) <= 60
    assert description.endswith(".")


def test_modern_sections_and_step_completion_criteria_are_present() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "# Codebase Design Skill" in text
    positions = [text.index(section) for section in REQUIRED_SECTIONS]
    assert positions == sorted(positions)

    steps = re.findall(
        r"^### \d+\..*?(?=^### \d+\.|^## )", text, re.MULTILINE | re.DOTALL
    )
    assert len(steps) == 6
    assert all("Done when" in step for step in steps)


def test_orientation_is_bounded_read_only_and_uses_native_tools() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for required in (
        "State a bounded selected area before inspecting code.",
        "one primary module, directory, feature slice, or entry point",
        "directly connected callers, consumers, or adapters",
        "`search_files`",
        "`read_file`",
        "before offering a design",
    ):
        assert required in normalized

    for prohibited in (
        "Do not use `write_file`",
        "`patch`",
        "`delegate_task`",
        "`cronjob`",
        "task trackers",
        "external systems",
    ):
        assert prohibited in normalized


def test_design_lenses_and_opt_in_comparison_are_explicit() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for concept in (
        "deep module",
        "interface size",
        "locality",
        "A seam is a boundary",
        "An adapter belongs at a boundary",
        "Identify leverage",
    ):
        assert concept in normalized

    assert "Do not automatically present multiple options." in normalized
    assert "only when the user explicitly asks to compare designs" in normalized
    assert "comparison is opt-in" in normalized


def test_provenance_credits_upstream_without_copying_or_local_paths() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "Matt Pocock" in text
    assert "MIT-licensed" in text
    assert "https://github.com/mattpocock/skills/blob/main/docs/engineering/codebase-design.md" in text
    assert "independently written" in text
    assert "/home/" not in text
    assert "C:\\Users\\" not in text
