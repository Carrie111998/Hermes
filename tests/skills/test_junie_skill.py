"""Contract tests for the bundled `junie` delegation skill.

SKILL.md is the artifact Hermes loads, so these run it through the real loader
(`agent.skill_utils.parse_frontmatter`) and assert the standards in
AGENTS.md ("Skill standards"): a short one-sentence description, human
contributor credited first, the modern section order, and platform gating that
matches what the prose actually asks the agent to run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.skill_utils import parse_frontmatter

SKILL_MD = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "autonomous-ai-agents"
    / "junie"
    / "SKILL.md"
)

REQUIRED_SECTION_ORDER = [
    "When to Use",
    "Prerequisites",
    "How to Run",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
]


@pytest.fixture(scope="module")
def skill() -> tuple[dict, str]:
    meta, body = parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    return meta, body


def test_frontmatter_loads_through_the_real_parser(skill):
    meta, body = skill
    assert meta.get("name") == "junie"
    assert body.strip(), "skill body is empty after frontmatter parsing"


def test_description_is_short_one_sentence(skill):
    meta, _ = skill
    description = str(meta.get("description", ""))
    # AGENTS.md: <= 60 chars, one sentence, ends with a period, no marketing words.
    assert len(description) <= 60, f"{len(description)} chars: {description!r}"
    assert description.endswith("."), description
    assert description.count(".") == 1, f"more than one sentence: {description!r}"
    banned = ("powerful", "comprehensive", "seamless", "advanced")
    lowered = description.lower()
    assert not [w for w in banned if w in lowered], description


def test_author_credits_the_human_first(skill):
    meta, _ = skill
    author = str(meta.get("author", ""))
    assert author, "author is required"
    first = author.split("+")[0].strip()
    assert first, author
    assert first.lower() != "hermes agent", (
        f"AGENTS.md requires the human contributor first, got {author!r}"
    )


def test_body_uses_the_modern_section_order(skill):
    _, body = skill
    sections = re.findall(r"^## (.+)$", body, re.MULTILINE)
    assert sections == REQUIRED_SECTION_ORDER, sections


def test_prose_points_at_native_hermes_tools(skill):
    _, body = skill
    # The headline interaction surface must be Hermes tools, not raw shell
    # utilities that Hermes already wraps.
    assert "terminal(" in body, "the skill must show the terminal tool call shape"
    for wrapped, native in (
        (r"\bgrep\b", "search_files"),
        (r"\bcat\b", "read_file"),
        (r"\bsed\b", "patch"),
    ):
        assert not re.search(wrapped, body), (
            f"use the native `{native}` tool instead of a wrapped shell utility"
        )


def test_platform_gating_matches_the_documented_commands(skill):
    meta, body = skill
    platforms = set(meta.get("platforms") or [])
    assert platforms, "platforms must be declared"
    # The interactive mode is tmux-based (POSIX), but it is optional and the
    # headless path works everywhere — so windows stays supported and the
    # install line must mention its Windows route.
    if "windows" in platforms:
        assert "PowerShell" in body, (
            "windows is declared supported, so document the Windows install route"
        )


def test_provider_settings_are_documented_as_config_not_env(skill):
    _, body = skill
    # AGENTS.md: behavioral settings live in config.yaml; .env is for secrets.
    assert "junie_acp:" in body and "config.yaml" in body
    assert not re.search(r"HERMES_JUNIE_ACP_\w+", body), (
        "point users at config.yaml, not HERMES_* env vars"
    )
    # JUNIE_API_KEY is a credential and stays an env var.
    assert "JUNIE_API_KEY" in body
