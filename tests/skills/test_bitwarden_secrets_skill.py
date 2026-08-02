"""Invariant tests for the bundled bitwarden-secrets skill.

Covers skills/security/bitwarden-secrets — the authoritative Bitwarden
Secrets Manager (bws) secrets-handling protocol. Tests assert the
contracts the maintainers hold for every bundled skill: valid
frontmatter, description within the 60-character hardline, required
sections present, and honest references to the hardening series rather
than to code that has not landed on main.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SKILL_DIR = REPO / "skills" / "security" / "bitwarden-secrets"
SKILL_MD = SKILL_DIR / "SKILL.md"

# The secrets-exfiltration hardening series — the docs may reference
# these PR numbers as the source of the contract, but must not claim
# their behavior or tests exist on main.
HARDENING_SERIES = {"77008", "77012", "77020", "77027", "77031", "77039"}


def _frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{skill_md} has no YAML frontmatter"
    return yaml.safe_load(match.group(1))


def test_skill_exists_with_frontmatter():
    assert SKILL_MD.exists(), f"missing {SKILL_MD}"
    fm = _frontmatter(SKILL_MD)
    assert fm["name"] == "bitwarden-secrets"
    assert fm["description"].strip()
    assert len(fm["description"]) <= 60, (
        f"description is {len(fm['description'])} chars (max 60)"
    )
    assert fm["description"].rstrip('"').endswith(".")
    platforms = fm.get("platforms")
    assert platforms, "missing platforms gating"
    assert set(platforms) <= {"linux", "macos", "windows"}


def test_required_sections_present():
    body = SKILL_MD.read_text(encoding="utf-8")
    for section in [
        "## When to Use",
        "## Protocol invariants (non-negotiable)",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]:
        assert section in body, f"missing required section {section}"


def test_rotation_is_user_action_only():
    """The agent must never perform rotation or handle the token value."""
    body = SKILL_MD.read_text(encoding="utf-8")
    assert "user action only" in body
    assert "never asks for the token value" in body
    assert "do not save it anywhere else in between" in body  # clipboard discipline


def test_no_claim_that_hardening_is_on_main():
    """The skill must not assert the hardened contract exists on main.

    Current main (as of the review) still reads/writes the plaintext
    bws_cache.json when encryption is disabled and defaults
    encrypted_cache.enabled to false. The skill may describe the
    contract the series implements, and may reference the series PRs,
    but must not claim the behavior or the gate test is on main.
    """
    body = SKILL_MD.read_text(encoding="utf-8")
    # Must scope the contract to the series.
    assert "secrets-exfiltration hardening series" in body
    assert "Until that series lands on" in body
    # Must not claim the gate test is present on main.
    assert "tests/test_secrets_exfiltration.py" in body
    assert "the no-exfiltration gate lands with the hardening series" in body


def test_referenced_series_prs_are_consistent():
    body = SKILL_MD.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"#(770\d\d)", body))
    assert mentioned and mentioned <= HARDENING_SERIES, (
        f"skill references PRs outside the hardening series: {mentioned - HARDENING_SERIES}"
    )


def test_skill_metadata_consistent_with_docs_page():
    """The mirrored docs page must carry the same description."""
    fm = _frontmatter(SKILL_MD)
    docs_page = (
        REPO
        / "website"
        / "docs"
        / "user-guide"
        / "skills"
        / "bundled"
        / "security"
        / "security-bitwarden-secrets.md"
    )
    assert docs_page.exists(), "missing mirrored docs page"
    page_fm = _frontmatter(docs_page)
    assert page_fm["description"] == fm["description"], (
        "docs page description diverged from SKILL.md"
    )
