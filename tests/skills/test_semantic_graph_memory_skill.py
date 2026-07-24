"""Skill frontmatter / structure tests for semantic-graph-memory."""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "semantic-graph-memory"
    / "SKILL.md"
)


def test_skill_description_and_sections():
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"^description:\s*(.*)$", text, re.MULTILINE)
    assert m is not None
    assert len(m.group(1).strip()) <= 60
    for section in (
        "# Semantic Graph Memory Skill",
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ):
        assert section in text
    for tool in (
        "delegate_task",
        "semantic_graph_begin_run",
        "semantic_graph_ingest",
        "semantic_graph_submit_fragment",
        "semantic_graph_finalize",
        "semantic_graph_evaluate_output",
    ):
        assert tool in text
    assert "Structure Extractor" in text
    assert "Evidence / Provenance Agent" in text
    assert "Skeptical Evaluator" in text
    # No chain-of-thought solicitation / no shell utility headlines.
    assert "hidden chain-of-thought" in text.lower() or "chain-of-thought" in text.lower()
    assert "Do not ask for or store hidden chain-of-thought" in text
    assert "`grep`" not in text
    assert "`cat`" not in text
