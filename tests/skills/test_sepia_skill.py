"""Tests for the ported sepia optional skill (de-AI writing)."""

import re
from pathlib import Path

import pytest
import yaml

STAGING_ROOT = Path(__file__).resolve().parents[2]
FALLBACK_ROOT = Path.home() / ".hermes" / "hermes-agent"


def _skill_dir() -> Path:
    staged = STAGING_ROOT / "optional-skills" / "creative" / "sepia"
    if staged.is_dir():
        return staged
    fallback = FALLBACK_ROOT / "optional-skills" / "creative" / "sepia"
    if fallback.is_dir():
        return fallback
    pytest.skip("sepia skill directory not found in staging or fallback")


SKILL_DIR = _skill_dir()
SKILL_MD = SKILL_DIR / "SKILL.md"

REFERENCE_FILES = [
    "references/discourse-pass.md",
    "references/narrative-pass.md",
    "references/professional-pass.md",
    "references/style-pass.md",
    "references/rubric.md",
    "references/model-fingerprints.md",
    "references/domains/dev-replies.md",
    "references/domains/postmortems.md",
    "references/domains/release-notes.md",
    "references/domains/tech-articles.md",
    "references/domains/tickets.md",
]


def _frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md missing YAML frontmatter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def test_frontmatter_parses():
    fm = _frontmatter()
    assert fm["name"] == "sepia"


def test_description_length_and_period():
    desc = _frontmatter()["description"]
    assert isinstance(desc, str)
    assert len(desc) <= 60, f"description is {len(desc)} chars"
    assert desc.endswith("."), "description must end with a period"


def test_platforms_present():
    platforms = _frontmatter().get("platforms")
    assert platforms, "platforms field is mandatory for optional skills"
    assert set(platforms) == {"linux", "macos", "windows"}


def test_license_mit_and_license_file():
    assert _frontmatter()["license"] == "MIT"
    lic = SKILL_DIR / "LICENSE.txt"
    assert lic.is_file(), "LICENSE.txt missing"
    assert "MIT License" in lic.read_text(encoding="utf-8")


def test_routing_table_reference_paths_exist():
    text = SKILL_MD.read_text(encoding="utf-8")
    paths = set(re.findall(r"`(references/[\w\-/]+\.md)`", text))
    assert paths, "no reference paths found in SKILL.md"
    for rel in sorted(paths):
        assert (SKILL_DIR / rel).is_file(), f"missing: {rel}"


def test_all_eleven_reference_files_exist_nonempty():
    for rel in REFERENCE_FILES:
        p = SKILL_DIR / rel
        assert p.is_file(), f"missing: {rel}"
        assert p.stat().st_size > 0, f"empty: {rel}"


def test_no_upstream_harness_residue():
    pattern = re.compile(r"allowed-tools|\.claude-plugin|AskUserQuestion", re.IGNORECASE)
    for i, line in enumerate(SKILL_MD.read_text(encoding="utf-8").splitlines(), 1):
        assert not pattern.search(line), f"harness residue on line {i}: {line!r}"


def test_related_skills_resolve():
    related = _frontmatter().get("metadata", {}).get("hermes", {}).get("related_skills", [])
    assert "humanizer" in related
    search_roots = [STAGING_ROOT, FALLBACK_ROOT / "skills"]
    found = any(
        root.is_dir() and any(p.name == "humanizer" and p.is_dir() for p in root.rglob("humanizer"))
        for root in search_roots
    )
    assert found, "related skill 'humanizer' not found under staging root or ~/.hermes/hermes-agent/skills"
