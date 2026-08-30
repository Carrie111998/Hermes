"""Tests for the opportunity-radar skill and its opportunity-radar blueprint.

Inspired by Energy's (getenergy.com) proactive suggestions — the assistant
watches the user's own activity streams (public posts, inbox, calendar) and
suggests timely cross-source actions, suggest-only.
"""
import re
from pathlib import Path

import yaml

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "productivity"
    / "opportunity-radar"
    / "SKILL.md"
)


def _frontmatter_and_body():
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert content.startswith("---")
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, "frontmatter must close with ---"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.end() + 3 :]
    return fm, body


def test_skill_file_exists():
    assert SKILL_PATH.is_file()


def test_frontmatter_required_fields():
    fm, _ = _frontmatter_and_body()
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in fm, f"missing frontmatter field: {field}"
    assert fm["name"] == "opportunity-radar"


def test_description_hardline():
    fm, _ = _frontmatter_and_body()
    desc = fm["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars; hardline is 60"
    assert desc.endswith(".")


def test_related_skills_resolve_in_repo():
    fm, _ = _frontmatter_and_body()
    repo_root = SKILL_PATH.parents[3]
    for name in fm["metadata"]["hermes"]["related_skills"]:
        hits = (
            list(repo_root.glob(f"skills/*/{name}/SKILL.md"))
            + list(repo_root.glob(f"optional-skills/*/{name}/SKILL.md"))
            + list(repo_root.glob(f"skills/*/*/{name}/SKILL.md"))
        )
        assert hits, f"related_skills entry does not resolve in-repo: {name}"


def test_setup_tick_split():
    """The skill must separate one-time setup from the recurring cron tick."""
    _, body = _frontmatter_and_body()
    assert "Setup (foreground, once)" in body
    assert "Tick (each scheduled run)" in body
    assert "cronjob(action=" in body, "must wire scheduling through the cronjob tool"


def test_suggest_only_boundary_explicit():
    """The radar must never act on the outside world — propose only."""
    _, body = _frontmatter_and_body()
    assert "never sends, replies, posts, books, or buys" in body
    assert "does not imply permission" in body
    assert "read-only against the outside world" in body


def test_state_discipline_present():
    """State-file source of truth + failed-read cutoff handling must be explicit."""
    _, body = _frontmatter_and_body()
    assert "source of truth" in body
    assert "suggestion ledger" in body
    assert "never advance a cutoff past data you did not actually read" in body


def test_source_verification_before_scheduling():
    _, body = _frontmatter_and_body()
    assert "Only after step 3 succeeded" in body
    assert "one bounded foreground read" in body


def test_cross_source_evidence_bar():
    """Suggestions need cross-source links or time triggers, capped per tick."""
    _, body = _frontmatter_and_body()
    assert "at most 3 suggestions" in body
    assert "two sources or a concrete time trigger" in body
    assert "dismissed suggestion stays dismissed" in body


def test_silent_path_explicit():
    _, body = _frontmatter_and_body()
    assert "[SILENT]" in body, "no-signal ticks must stay silent"


def test_steps_have_completion_criteria():
    _, body = _frontmatter_and_body()
    steps = re.findall(r"^### \d+\..*?(?=^### \d+\.|^## )", body, re.MULTILINE | re.DOTALL)
    assert len(steps) >= 6
    for step in steps:
        assert "Done when" in step, f"step missing completion criterion: {step[:60]!r}"


def test_opportunity_radar_blueprint_registered():
    from cron.blueprint_catalog import CATALOG

    bp = next((b for b in CATALOG if b.key == "opportunity-radar"), None)
    assert bp is not None, "opportunity-radar blueprint missing from catalog"
    assert "opportunity-radar" in bp.skills, "blueprint must load the skill"
    slot_names = {s.name for s in bp.slots}
    assert {"sources", "focus", "time", "recurrence", "deliver"} <= slot_names
    assert "[SILENT]" in bp.prompt_template, "silent path must be explicit"
    assert "{sources}" in bp.prompt_template and "{focus}" in bp.prompt_template
    assert "never" in bp.prompt_template and "act" in bp.prompt_template, (
        "suggest-only boundary must survive into the cron prompt"
    )


def test_opportunity_radar_blueprint_fills():
    """fill_blueprint must produce a valid cron job kwargs dict."""
    from cron.blueprint_catalog import CATALOG, fill_blueprint

    bp = next(b for b in CATALOG if b.key == "opportunity-radar")
    job = fill_blueprint(
        bp,
        {
            "sources": "my X posts and my inbox",
            "focus": "intros and unanswered asks",
            "time": "09:30",
            "recurrence": "weekdays",
            "deliver": "origin",
        },
    )
    assert "my X posts and my inbox" in job["prompt"]
    assert "intros and unanswered asks" in job["prompt"]
    fields = job["schedule"].split()
    assert len(fields) == 5, f"invalid cron expr: {job['schedule']}"
    assert fields[0] == "30" and fields[1] == "9"
    assert fields[4] == "1-5"
