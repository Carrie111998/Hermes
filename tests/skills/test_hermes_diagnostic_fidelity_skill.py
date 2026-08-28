"""Authoring + content contract for the hermes-diagnostic-fidelity skill.

The generic sweep in ``test_authoring_standards.py`` covers the mechanical
frontmatter rules for every skill. These tests pin the parts that are specific
to this skill: the tier/placement decision, the shell-utility prose ban, and
the load-bearing sections a reader relies on (the core principle, the
revert-check step, and the completion criteria that make the procedure
checkable rather than aspirational).
"""
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "optional-skills" / "software-development" / "hermes-diagnostic-fidelity"
SKILL_MD = SKILL_DIR / "SKILL.md"


@pytest.fixture(scope="module")
def content() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(content: str) -> dict:
    assert content.startswith("---"), "frontmatter must start at byte 0"
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "unclosed frontmatter"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    assert isinstance(fm, dict)
    return fm


def test_skill_is_optional_tier():
    """Contributor-facing repo-maintenance skill — not a daily driver."""
    assert SKILL_MD.exists(), f"missing skill at {SKILL_MD.relative_to(REPO)}"
    assert not (
        REPO / "skills" / "software-development" / "hermes-diagnostic-fidelity"
    ).exists(), "skill must live in optional-skills/, not bundled skills/"


def test_description_within_index_window(frontmatter):
    """The trigger must survive the system-prompt index truncation at 57."""
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"{len(desc)} chars (hardline 60)"
    assert desc.endswith(".")
    assert "hermes-diagnostic-fidelity" not in desc, "don't repeat the skill name"


def test_related_skills_resolve_in_repo(frontmatter):
    """Every related skill must exist in this tree, not only user-local."""
    names = {
        p.parent.name
        for p in list(REPO.glob("skills/**/SKILL.md"))
        + list(REPO.glob("optional-skills/**/SKILL.md"))
    }
    dangling = [
        rs
        for rs in frontmatter["metadata"]["hermes"]["related_skills"]
        if rs not in names
    ]
    assert not dangling, f"dangling related_skills: {dangling}"


def test_prose_names_hermes_tools_not_raw_shell(content: str):
    """AGENTS.md hardline #2: wrapped shell utilities must not headline prose."""
    body = content.split("\n---\n", 1)[1]
    for banned in (r"`grep`", r"`cat`", r"`head`", r"`tail`", r"`sed`", r"`awk`", r"`find`"):
        assert banned not in body, f"prose names a wrapped shell utility: {banned}"
    for expected in ("`terminal`", "`search_files`", "`read_file`"):
        assert expected in body, f"prose should reference {expected}"


def test_core_principle_is_stated(content: str):
    """The delegate-don't-re-derive rule is the whole point of the skill."""
    assert "## Core Principle" in content
    assert "never re-derive" in content


def test_revert_check_step_present(content: str):
    """A regression test that passes both ways is a no-op — say so."""
    assert "prove they bite" in content
    assert "one cluster per defect" in content
    assert "passes both ways is a no-op" in content


def test_every_procedure_step_has_completion_criterion(content: str):
    """Numbered steps must end in something checkable."""
    procedure = content.split("## Procedure", 1)[1].split("## Pitfalls", 1)[0]
    steps = re.findall(r"^\d+\. \*\*(.+?)\*\*", procedure, re.MULTILINE)
    assert len(steps) >= 6, f"expected the full procedure, found {len(steps)} steps"
    assert procedure.count("Done when") == len(steps), (
        f"{len(steps)} steps but {procedure.count('Done when')} completion criteria"
    )


def test_modern_section_order(content: str):
    """Sections appear in the AGENTS.md hardline #5 order."""
    order = [
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = [content.index(s) for s in order]
    assert positions == sorted(positions), "sections out of the modern order"


def test_no_machine_local_paths(content: str):
    """A baked-in home directory breaks the skill for every other user."""
    assert not re.search(r"/home/(?!runner\b)[a-z0-9_-]+/", content)
    assert "/.hermes/hermes-agent/" not in content


def test_pinned_toolchain_pitfall_is_present(content: str):
    """Regression: verifying with an unpinned tool fabricated a diagnostic.

    An `npx --yes -p pkg@5` fallback resolved a cached 5.9.3 against absent
    dependencies while the project pinned 6.0.3, producing a missing-module
    error that belonged to the harness. The skill must keep warning about it
    and must pair the warning with a checkable verification item.
    """
    pitfalls = content.split("## Pitfalls", 1)[1].split("## Verification", 1)[0]
    assert "pinned" in pitfalls
    assert "npx" in pitfalls
    assert "./node_modules/.bin/" in pitfalls
    assert "--version" in pitfalls

    verification = content.split("## Verification", 1)[1]
    assert "pinned binary" in verification
    assert "lockfile" in verification


def test_baseline_must_be_remeasured_after_tool_change(content: str):
    """Comparing against a baseline from the same broken harness is not proof."""
    pitfalls = content.split("## Pitfalls", 1)[1].split("## Verification", 1)[0]
    assert "Two bad runs are not a baseline" in pitfalls
    assert "re-measured" in content.split("## Verification", 1)[1]


def test_size_is_reviewable(content: str):
    """Target ~200 lines for a complex skill; hard ceiling is 100k chars."""
    assert len(content) <= 100_000
    assert len(content.splitlines()) <= 200
