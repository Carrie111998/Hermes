"""Structural contract tests for the bundled eli5 visual-explainer skill.

Contracts are anchored to specific parsed structures (frontmatter fields,
numbered Procedure items, Artifact-contract bullets, generated docs page),
not to free-floating phrase scans, so harmless rewording cannot break them
and an unrelated word cannot accidentally satisfy them.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "creative" / "eli5"
SKILL_PATH = SKILL_DIR / "SKILL.md"
GENERATED_PAGE = (
    REPO_ROOT
    / "website"
    / "docs"
    / "user-guide"
    / "skills"
    / "bundled"
    / "creative"
    / "creative-eli5.md"
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


def _load_skill() -> tuple[dict, str]:
    """Parse SKILL.md with the production frontmatter parser (stdlib-only reuse)."""
    from tools.skills_tool import _parse_frontmatter

    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---"), "SKILL.md must start with YAML frontmatter"
    frontmatter, body = _parse_frontmatter(content)
    assert isinstance(frontmatter, dict) and frontmatter, "frontmatter must parse to a mapping"
    return frontmatter, body


def _section(body: str, heading: str) -> str:
    """Return the text under `heading` up to the next heading of any level."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    assert start is not None, f"missing required section: {heading}"
    collected: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        collected.append(line)
    return "\n".join(collected)


def _procedure_items(body: str) -> dict[int, str]:
    """Parse the ordered Procedure list into {index: full item text}."""
    proc = _section(body, "## Procedure")
    items: dict[int, list[str]] = {}
    current = None
    for line in proc.splitlines():
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if m:
            current = int(m.group(1))
            items[current] = [m.group(2)]
        elif current is not None and line.strip() and not line.startswith("#"):
            items[current].append(line.strip())
    return {k: " ".join(v) for k, v in items.items()}


def _contract_bullets(body: str) -> list[str]:
    """Parse the Artifact-contract bullet list."""
    block = _section(body, "### Artifact contract")
    return [line.lstrip("- ").strip() for line in block.splitlines() if line.startswith("- ")]


# ---------------------------------------------------------------------------
# Frontmatter contracts
# ---------------------------------------------------------------------------


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


def test_no_runtime_dependencies_declared():
    """The skill must declare zero dependencies and zero required env vars."""
    fm, _ = _load_skill()
    assert not fm.get("dependencies"), f"unexpected dependencies: {fm.get('dependencies')}"
    prereq = fm.get("prerequisites")
    env_vars = prereq.get("env_vars") if isinstance(prereq, dict) else None
    assert not env_vars, f"unexpected required env vars: {env_vars}"


# ---------------------------------------------------------------------------
# Section structure
# ---------------------------------------------------------------------------


def test_body_uses_modern_section_order():
    _, body = _load_skill()
    positions = []
    for section in REQUIRED_SECTIONS:
        assert section in body, f"missing required section: {section}"
        positions.append(body.index(section))
    assert positions == sorted(positions), "sections appear out of order"


def test_related_skills_resolve_in_repo():
    """Every related_skills entry must exist in-repo (bundled or optional)."""
    fm, _ = _load_skill()
    related = ((fm.get("metadata") or {}).get("hermes") or {}).get("related_skills") or []
    assert related, "related_skills should point at sibling creative skills"
    for name in related:
        found = any(
            p.parent.name == name
            for base in ("skills", "optional-skills")
            for p in (REPO_ROOT / base).rglob("SKILL.md")
        )
        assert found, f"related skill does not resolve in-repo: {name}"


# ---------------------------------------------------------------------------
# Procedure contracts (structure-scoped, per numbered item)
# ---------------------------------------------------------------------------


def test_ground_facts_item_names_native_tools_and_forbids_invention():
    _, body = _load_skill()
    first = _procedure_items(body)[1]
    assert first.startswith("**Ground the facts.**")
    for tool in ("read_file", "search_files", "web_extract"):
        assert f"`{tool}`" in first, f"grounding step must name `{tool}`"
    assert "Do not invent" in first, "grounding step must forbid invented facts"


def test_preserve_identifiers_item_demands_verbatim_quotes():
    _, body = _load_skill()
    third = _procedure_items(body)[3]
    assert third.startswith("**Preserve identifiers exactly.**")
    assert "verbatim" in third


def test_write_artifact_item_demands_absolute_selfcontained_html():
    _, body = _load_skill()
    fourth = _procedure_items(body)[4]
    assert fourth.startswith("**Write the artifact.**")
    assert "`write_file`" in fourth, "artifact step must use the native write_file tool"
    assert ".html" in fourth and "self-contained" in fourth
    examples = re.findall(r"`([^`]+)`", fourth)
    html_examples = [e for e in examples if e.endswith(".html") and "/" in e]
    assert html_examples, "artifact step must show an example output file path"
    for example in html_examples:
        assert example.startswith("/") and len(example) > 1, (
            f"example output path must be POSIX-absolute: {example!r}"
        )


def test_report_item_requires_bare_path_not_backticked():
    """Gateway delivery extracts bare paths only; inline-code paths are ignored."""
    _, body = _load_skill()
    fifth = _procedure_items(body)[5]
    lowered = fifth.lower()
    assert "bare absolute path" in lowered
    assert "backticks" in lowered, "step must warn against wrapping the path in backticks"


# ---------------------------------------------------------------------------
# Artifact-contract bullets
# ---------------------------------------------------------------------------


def test_contract_bullet_selfcontained_single_file():
    _, body = _load_skill()
    assert any(b.startswith("Single self-contained") for b in _contract_bullets(body))


def test_contract_bullet_no_external_assets():
    _, body = _load_skill()
    bullets = [b.lower() for b in _contract_bullets(body)]
    assert any("no cdn" in b and "offline" not in b for b in bullets), (
        "a bullet must prohibit CDNs/external assets outright"
    )


def test_contract_bullet_tldr_strip():
    _, body = _load_skill()
    assert any("tl;dr strip" in b.lower() for b in _contract_bullets(body))


def test_contract_bullet_bounds_sections_three_to_seven():
    _, body = _load_skill()
    assert any("3 to 7 numbered sections" in b for b in _contract_bullets(body))


def test_contract_bullet_labels_unverified_claims():
    _, body = _load_skill()
    assert any("[unverified]" in b for b in _contract_bullets(body))


# ---------------------------------------------------------------------------
# No unconverted external-plugin surface
# ---------------------------------------------------------------------------


def test_no_unconverted_claude_plugin_markers():
    """Reject Claude-plugin conversion leftovers ($ARGUMENTS context blocks,
    vendor URLs). These are precise markers; ordinary prose stays untouched."""
    _, body = _load_skill()
    lowered = body.lower()
    for marker in ("$arguments", "<context>", "claude.ai", "anthropic.com"):
        assert marker not in lowered, f"unconverted Claude-plugin marker found: {marker}"


# ---------------------------------------------------------------------------
# Verification-section contracts
# ---------------------------------------------------------------------------


def test_verification_offline_check_has_full_remote_surface():
    """The offline check must cover src/href, CSS url(), @import, srcset,
    protocol-relative URLs, while allowing same-page hash anchors."""
    _, body = _load_skill()
    verification = _section(body, "## Verification")
    for token in ('href="#', "url(...)", "@import", "srcset", "//"):
        assert token in verification, f"offline check must mention {token!r}"


# ---------------------------------------------------------------------------
# Generated docs page must stay in sync with SKILL.md
# ---------------------------------------------------------------------------


def test_generated_page_reflects_current_skill_version_and_content():
    page_text = GENERATED_PAGE.read_text(encoding="utf-8")
    fm, body = _load_skill()
    assert f"| Version | `{fm['version']}` |" in page_text, (
        "generated page version row is stale; rerun website/scripts/generate-skill-docs.py"
    )
    closing_sentence = "Never claim visual verification that did not happen."
    assert closing_sentence in body
    assert closing_sentence in page_text, (
        "generated page body is stale relative to SKILL.md; rerun website/scripts/generate-skill-docs.py"
    )


# ---------------------------------------------------------------------------
# Production discovery path (hermetic)
# ---------------------------------------------------------------------------


def test_production_discovery_lists_eli5_with_expected_metadata(tmp_path, monkeypatch):
    """tools.skills_tool._find_all_skills must discover eli5 through the real
    scanner, in a hermetic temp skills tree with external dirs patched out."""
    import tools.skills_tool as skills_tool

    category_dir = tmp_path / "creative"
    category_dir.mkdir(parents=True)
    shutil.copytree(SKILL_DIR, category_dir / "eli5")

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])

    entries = [
        s
        for s in skills_tool._find_all_skills(skip_disabled=True)
        if s["name"] == "eli5"
    ]

    assert len(entries) == 1, f"expected exactly one eli5 discovery, got {entries}"
    entry = entries[0]
    assert entry["category"] == "creative"
    assert entry["description"] == "Explain any topic as a simple visual HTML page."
