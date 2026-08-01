"""Invariants for the research-paper-writing bundled skill size split."""

import re
from pathlib import Path

from tools.skill_manager_tool import MAX_SKILL_CONTENT_CHARS

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "research" / "research-paper-writing" / "SKILL.md"
REFS = REPO_ROOT / "skills" / "research" / "research-paper-writing" / "references"


def test_skill_md_under_serving_limit():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert len(text) < MAX_SKILL_CONTENT_CHARS
    assert SKILL_MD.stat().st_size < MAX_SKILL_CONTENT_CHARS + 4096


def test_description_authoring_limit():
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"^description:\s*(.*)$", text, re.MULTILINE)
    assert match is not None
    desc = match.group(1).strip().strip("\"'")
    assert len(desc) <= 60
    assert desc.endswith(".")


def test_standard_sections_and_reference_split():
    text = SKILL_MD.read_text(encoding="utf-8")
    for section in (
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ):
        assert section in text, section

    assert "references/latex-and-templates.md" in text
    assert "references/hermes-integration.md" in text
    assert (REFS / "latex-and-templates.md").is_file()
    assert (REFS / "hermes-integration.md").is_file()
    # Bulky LaTeX guidance lives in the reference, not the served skill body.
    latex_ref = (REFS / "latex-and-templates.md").read_text(encoding="utf-8")
    assert "SciencePlots" in latex_ref or "latexmk" in latex_ref
