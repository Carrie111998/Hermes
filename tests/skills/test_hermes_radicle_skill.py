"""
Smoke tests for the hermes-radicle optional skill.

Validates hardline SKILL.md standards from AGENTS.md: short description,
required frontmatter, platforms gating, modern section order, generic
(non-personal) examples, and native-tool guidance (no grep in prose).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "devops"
    / "hermes-radicle"
)
SKILL_MD = SKILL_DIR / "SKILL.md"

REQUIRED_SECTIONS = (
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_text: str) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_text, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


def test_skill_dir_exists() -> None:
    assert SKILL_DIR.is_dir(), f"missing skill dir: {SKILL_DIR}"


def test_skill_md_present() -> None:
    assert SKILL_MD.is_file()


def test_description_under_60_chars(frontmatter: dict) -> None:
    desc = frontmatter["description"]
    assert isinstance(desc, str)
    assert desc.endswith("."), f"description must end with a period: {desc!r}"
    assert len(desc) <= 60, f"description is {len(desc)} chars (limit ≤60): {desc!r}"


def test_name_matches_dir(frontmatter: dict) -> None:
    assert frontmatter["name"] == "hermes-radicle"


def test_has_required_frontmatter_fields(frontmatter: dict) -> None:
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in frontmatter, f"missing required field: {field}"


def test_platforms_are_posix_desktop(frontmatter: dict) -> None:
    platforms = frontmatter["platforms"]
    assert set(platforms) == {"linux", "macos"}
    assert "windows" not in platforms


def test_license_mit(frontmatter: dict) -> None:
    assert frontmatter["license"] == "MIT"


def test_author_credits_contributor(frontmatter: dict) -> None:
    assert "Joey Stanford" in frontmatter["author"]


def test_required_sections_present(skill_text: str) -> None:
    for heading in REQUIRED_SECTIONS:
        assert heading in skill_text, f"missing section: {heading}"


def test_no_personal_repo_embedding(skill_text: str) -> None:
    """Official skills must be reusable — no personal RID, forge URL, or local path."""
    body = skill_text.split("---", 2)[-1]
    forbidden = (
        "rinchen/hermes-radicle",
        "rad:z3Y6EqELoo7SUYhk4WJdJS3zzJoVK",
        "rad-enable-repo",
        "decentralized-manifesto",
        "~/repos/",
    )
    for needle in forbidden:
        assert needle not in body, f"personal/example-specific embedding found: {needle!r}"


def test_no_grep_in_skill_prose(skill_text: str) -> None:
    """AGENTS.md: name native Hermes tools, not shell utilities like grep."""
    body = skill_text.split("---", 2)[-1]
    assert not re.search(r"\bgrep\b", body), "SKILL.md must not name grep in prose"


def test_documents_pushurl_fallback_correctly(skill_text: str) -> None:
    """A branch refspec must not be presented as skipping stacked Radicle pushurls."""
    assert "set-url --delete --push" in skill_text
    assert "distinct remotes" in skill_text.lower() or "distinct remote" in skill_text.lower()
    # Guard the incorrect escape hatch called out in review.
    assert not re.search(
        r"Fall back to `git push origin <branch>` \(GitHub",
        skill_text,
    )


def test_points_at_terminal_tool(skill_text: str) -> None:
    assert "`terminal`" in skill_text
