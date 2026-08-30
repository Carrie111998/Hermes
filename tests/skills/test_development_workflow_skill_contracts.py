"""Behavioral instruction contracts for core development skills.

These tests assert stable policy invariants rather than snapshotting prose.
"""

from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"


def _skill(name: str) -> Path:
    hits = list(SKILLS_ROOT.glob(f"*/{name}/SKILL.md"))
    assert len(hits) == 1, f"expected one bundled skill named {name}, got {hits}"
    return hits[0]


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    match = re.search(r"\n---\s*\n", text[4:])
    assert match, f"missing frontmatter terminator: {path}"
    return yaml.safe_load(text[4 : match.start() + 4])


def _normalized_skill(name: str) -> str:
    return " ".join(_skill(name).read_text(encoding="utf-8").split())


def test_changed_skill_frontmatter_and_related_skills_resolve():
    for name in (
        "test-driven-development",
        "systematic-debugging",
        "requesting-code-review",
        "simplify-code",
    ):
        path = _skill(name)
        frontmatter = _frontmatter(path)
        description = frontmatter["description"]
        assert len(description) <= 60
        assert description.endswith(".")
        assert frontmatter["platforms"]
        for related in frontmatter["metadata"]["hermes"].get("related_skills", []):
            assert list(SKILLS_ROOT.glob(f"*/{related}/SKILL.md")), (
                f"unresolved related skill {related!r} in {path}"
            )


def test_bundled_skills_have_no_removed_orchestration_reference():
    offenders = []
    for path in SKILLS_ROOT.glob("**/SKILL.md"):
        if "subagent-driven-development" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"removed skill reference remains in: {offenders}"


def test_tdd_supports_honest_adaptation_without_discarding_valid_code():
    text = _normalized_skill("test-driven-development")
    for concept in (
        "alternative evidence before editing",
        "Do not delete correct production code",
        "existing green suite is the baseline",
        "focused check passes",
    ):
        assert concept in text
    assert "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST" not in text
    assert "Delete code. Start over" not in text


def test_review_is_risk_proportional_and_not_a_release_action():
    text = _normalized_skill("requesting-code-review")
    for concept in (
        "small low-risk change",
        "Decide whether independent review is warranted",
        "Do not commit, push, merge, deploy",
        "High-Assurance Exact-Candidate Review",
    ):
        assert concept in text
    for removed_contract in ("delivery_task", "[verified]", "2+ file edits"):
        assert removed_contract not in text


def test_simplify_bounds_parallel_review_and_keeps_writers_separate():
    text = _normalized_skill("simplify-code")
    assert "Run at most three reviewers in parallel" in text
    assert "read-only boundary" in text
    assert "no two writers own overlapping files" in text
    assert "Four narrow reviewers" not in text
    assert "four focused reviewers" not in text
