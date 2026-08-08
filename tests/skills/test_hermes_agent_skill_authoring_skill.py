"""Tests for the hermes-agent-skill-authoring SKILL.md and its references.

Validates:
  - Frontmatter: required fields, ≤60-char description, ends with period,
    trigger self-contained in first 57 characters
  - Body structure: section presence, no machine-local paths
  - Dynamic Loading Rules: present with 2+ references/*.md, paths resolve,
    no load-all phrasing, no orphans
  - Safety & Enforcement template reference resolves
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "software-development"
    / "hermes-agent-skill-authoring"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"

BANNED_MARKETING = {"powerful", "comprehensive", "seamless", "advanced", "robust", "end-to-end"}


@pytest.fixture(scope="module")
def skill_source() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_source) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_source, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


@pytest.fixture(scope="module")
def reference_files() -> list[Path]:
    if not REFERENCES.is_dir():
        return []
    return sorted(p for p in REFERENCES.rglob("*.md") if p.is_file())


# ── Frontmatter ──────────────────────────────────────────────────────────────

class TestFrontmatter:
    def test_skill_dir_exists(self) -> None:
        assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"

    def test_starts_with_frontmatter(self, skill_source: str) -> None:
        assert skill_source.startswith("---"), "SKILL.md must start with ---"

    def test_required_fields_present(self, frontmatter: dict) -> None:
        for field in ("name", "description", "version", "author", "license", "platforms"):
            assert field in frontmatter, f"missing required field: {field}"

    def test_metadata_tags_present(self, frontmatter: dict) -> None:
        meta = frontmatter.get("metadata", {})
        assert "hermes" in meta, "missing metadata.hermes"
        assert "tags" in meta["hermes"], "missing metadata.hermes.tags"

    def test_description_under_60_chars(self, frontmatter: dict) -> None:
        desc = frontmatter["description"]
        assert len(desc) <= 60, f"description is {len(desc)} chars (limit ≤60): {desc!r}"

    def test_description_ends_with_period(self, frontmatter: dict) -> None:
        desc = frontmatter["description"]
        assert desc.endswith("."), f"description must end with a period: {desc!r}"

    def test_description_trigger_in_first_57(self, frontmatter: dict) -> None:
        """Trigger/capability must be self-contained in the effective window."""
        desc = frontmatter["description"]
        if len(desc) > 57:
            window = desc[:57]
            # The first 57 chars must contain a capability verb, not just setup
            assert any(
                word in window.lower()
                for word in ("author", "create", "track", "review", "manage", "search", "build", "run", "generate")
            ), f"Description trigger not self-contained in first 57 chars: {window!r}"

    def test_no_author_hermes_alone(self, frontmatter: dict) -> None:
        author = frontmatter.get("author", "")
        assert "Hermes Agent" not in author or "," in author or "(" in author, (
            "author must credit the human first: {author!r}"
        )

    def test_platforms_present(self, frontmatter: dict) -> None:
        assert "platforms" in frontmatter
        assert isinstance(frontmatter["platforms"], list)

    def test_skip_banned_marketing_words(self, frontmatter: dict) -> None:
        """Use judgment for context-dependent words — flag not auto-fail."""
        desc = frontmatter["description"].lower()
        found = [w for w in BANNED_MARKETING if w in desc]
        if found:
            pytest.skip(f"description contains flagged terms (review): {found}")


# ── Body structure ────────────────────────────────────────────────────────────

class TestBodyStructure:
    def test_has_when_to_use_section(self, skill_source: str) -> None:
        assert re.search(r"^## When to Use\s*$", skill_source, re.M), "missing ## When to Use"

    def test_has_verification_section(self, skill_source: str) -> None:
        assert re.search(r"^## Verification Checklist\s*$", skill_source, re.M), (
            "missing ## Verification Checklist"
        )

    def test_no_machine_local_paths(self, skill_source: str) -> None:
        # Exclude teaching/anti-pattern examples: <you>, <category>, ~/.hermes/skills/ (doc path)
        bad = re.findall(
            r"`(/Users/[^`/<]+[^`]*|/home/[^`/<]+[^`]*|~/?\.hermes/skills/[^`]+)`",
            skill_source,
        )
        # Filter out patterns with angle brackets (teaching examples) or <category> variables
        bad = [p for p in bad if "<" not in p and ">" not in p]
        assert not bad, f"machine-local paths in skill: {bad}"

    def test_skill_size_under_limit(self, skill_source: str) -> None:
        assert len(skill_source) <= 100_000, (
            f"SKILL.md is {len(skill_source)} chars (limit 100,000)"
        )


# ── Dynamic Loading Rules (2+ references/*.md present) ───────────────────────

class TestDynamicLoadingRules:
    def test_multi_ref_skills_have_section(self, skill_source: str, reference_files: list[Path]) -> None:
        if len(reference_files) < 2:
            pytest.skip("fewer than 2 reference files — section optional")
        assert re.search(r"^## Dynamic Loading Rules\s*$", skill_source, re.M), (
            "skills with 2+ references/*.md must include ## Dynamic Loading Rules"
        )

    def test_section_declares_scoped_loading(self, skill_source: str, reference_files: list[Path]) -> None:
        if len(reference_files) < 2:
            pytest.skip("section not required")
        m = re.search(r"## Dynamic Loading Rules\n(.*?)(?=\n## |\Z)", skill_source, re.S)
        assert m, "Dynamic Loading Rules section missing"
        body = m.group(1).lower()
        signals = ("load no", "only when", "when the task", "never pre-load", "default", "heuristic")
        assert any(s in body for s in signals), (
            "section must state default/scoped loading, not unbounded read of references/"
        )

    def test_backticked_reference_paths_exist(self, skill_source: str) -> None:
        # Exclude glob patterns like `references/*.md`
        mentioned = set(re.findall(r"`(references/[^`*]+)`", skill_source))
        # Exclude convention/teaching placeholders containing ellipsis
        mentioned = {m for m in mentioned if "..." not in m}
        if not mentioned:
            pytest.skip("no backticked references/ paths in SKILL.md")
        missing = []
        for rel in mentioned:
            rel_path = rel.split("#", 1)[0]
            if not (SKILL_DIR / rel_path).is_file():
                missing.append(rel_path)
        assert not missing, f"SKILL.md references missing files: {missing}"

    def test_every_reference_file_is_mapped(self, skill_source: str, reference_files: list[Path]) -> None:
        if len(reference_files) < 2:
            pytest.skip("section not required")
        orphans = []
        for path in reference_files:
            rel = path.relative_to(SKILL_DIR).as_posix()
            if rel not in skill_source and path.name not in skill_source:
                orphans.append(rel)
        assert not orphans, f"orphan references (not named in SKILL.md): {orphans}"

    def test_no_machine_local_reference_paths(self, skill_source: str) -> None:
        bad = re.findall(
            r"`(/Users/[^`/<]+[^`]*|/home/[^`/<]+[^`]*|~/?\.hermes/skills/[^`]+)`",
            skill_source,
        )
        bad = [p for p in bad if "<" not in p and ">" not in p]
        assert not bad, f"machine-local paths in skill: {bad}"

    def test_avoids_load_all_phrasing(self, skill_source: str) -> None:
        text = skill_source.lower()
        banned = [
            "read all of references",
            "load all references",
            "load the entire references",
            "read every reference",
        ]
        hits = [b for b in banned if b in text]
        assert not hits, f"load-all anti-pattern phrasing found: {hits}"


# ── Safety template access ───────────────────────────────────────────────────

class TestSafetyTemplate:
    def test_safety_reference_exists(self) -> None:
        safepath = REFERENCES / "safety-enforcement-template.md"
        assert safepath.is_file(), f"missing reference: {safepath}"

    def test_safety_referenced_in_skill_md(self, skill_source: str) -> None:
        assert "safety-enforcement-template.md" in skill_source, (
            "SKILL.md must reference the safety template"
        )


# ── Dynamic Loading template access ──────────────────────────────────────────

class TestDynamicLoadingTemplate:
    def test_dynamic_loading_reference_exists(self) -> None:
        dlpath = REFERENCES / "dynamic-loading-rules-template.md"
        assert dlpath.is_file(), f"missing reference: {dlpath}"

    def test_dynamic_loading_referenced_in_skill_md(self, skill_source: str) -> None:
        assert "dynamic-loading-rules-template.md" in skill_source, (
            "SKILL.md must reference the dynamic loading template"
        )


# ── Frontmatter consistency ──────────────────────────────────────────────────

class TestVersionSemver:
    def test_version_is_semver(self, frontmatter: dict) -> None:
        v = frontmatter.get("version", "")
        assert re.match(r"^\d+\.\d+\.\d+$", v), f"version not semver: {v!r}"