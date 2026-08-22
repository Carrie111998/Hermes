from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "creative" / "eli5" / "SKILL.md"


def _load_skill() -> tuple[dict, str]:
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md frontmatter is malformed"
    frontmatter = yaml.safe_load(parts[1]) or {}
    body = parts[2]
    return frontmatter, body


def test_skill_file_exists():
    assert SKILL_PATH.is_file(), f"missing skill file: {SKILL_PATH}"


def test_frontmatter_declares_eli5_name():
    fm, _ = _load_skill()
    assert fm.get("name") == "eli5"


def test_description_is_single_short_sentence():
    fm, _ = _load_skill()
    description = str(fm.get("description", "")).strip()
    assert description, "description must not be empty"
    assert len(description) <= 60, f"description too long ({len(description)} chars): {description!r}"
    assert description.endswith("."), "description must end with a period"
    sentences = re.split(r"(?<=[.!?])\s+", description)
    assert len(sentences) == 1, "description must be exactly one sentence"


def test_body_uses_modern_section_order():
    _, body = _load_skill()
    required = [
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = []
    for section in required:
        assert section in body, f"missing required section: {section}"
        positions.append(body.index(section))
    assert positions == sorted(positions), "sections appear out of order"


def test_procedure_requires_absolute_selfcontained_html_path():
    _, body = _load_skill()
    assert "absolute path" in body, "procedure must demand an absolute output path"
    assert ".html" in body, "procedure must name the .html deliverable"
    assert "self-contained" in body, "output must be self-contained"


def test_procedure_preserves_exact_identifiers():
    _, body = _load_skill()
    assert "exactly" in body.lower(), "procedure must demand exact preservation of identifiers, commands, URLs, and code"


def test_procedure_demands_evidence_over_invention():
    _, body = _load_skill()
    assert "do not invent" in body.lower(), "skill must forbid invented architecture or facts"


def test_no_claude_or_anthropic_runtime_dependency():
    _, body = _load_skill()
    lowered = body.lower()
    assert "claude" not in lowered, "skill must not depend on Claude"
    assert "anthropic" not in lowered, "skill must not depend on Anthropic"


def test_no_new_runtime_surface_required():
    _, body = _load_skill()
    lowered = body.lower()
    assert "mcp" not in lowered, "skill must not require an MCP server"
    assert "api key" not in lowered, "skill must not require an API key"


def test_procedure_uses_native_hermes_tools():
    _, body = _load_skill()
    assert "`write_file`" in body, "procedure must use the native write_file tool"
    assert "`read_file`" in body, "procedure must use the native read_file tool"
    assert "`terminal`" not in body, "skill must not need shell commands for the core workflow"
    for shell_utility in ("`grep`", "`cat`", "`sed`", "`awk`", "`find`", "`ls`"):
        assert shell_utility not in body, f"use native Hermes tools instead of {shell_utility}"


def test_no_remote_assets_required():
    _, body = _load_skill()
    lowered = body.lower()
    assert "cdn" in lowered, "skill must explicitly forbid CDN dependencies"
    assert "no external" in lowered, "skill must forbid external runtime dependencies"


def test_visual_sequence_contract_present():
    _, body = _load_skill()
    assert "TL;DR" in body, "artifact contract must include a TL;DR"
    assert "3 to 7" in body, "artifact contract must bound the section count at 3 to 7"


def test_uncertainty_must_be_labeled():
    _, body = _load_skill()
    assert "[unverified]" in body, "skill must require labeling uncertain claims"


def test_deliverable_mode_path_mentioned():
    _, body = _load_skill()
    assert "deliverable" in body.lower(), "skill should explain gateway deliverable handling of the .html path"
