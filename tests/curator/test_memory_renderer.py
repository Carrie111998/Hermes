"""Unit tests for curator.memory_renderer."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from curator.memory_renderer import render, _stable_prefix

NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _basic_audit():
    return {
        "agent": "scout",
        "runs_total": 21,
        "runs_ok": 20,
        "runs_fail": 1,
        "avg_duration_s": 8.4,
        "event_type_counts": {"cron_completed": 20, "cron_failed": 1, "mailbox_message": 5},
    }


def _basic_drawers():
    return {
        "agent": "scout",
        "drawer_count_total": 12,
        "drawers_by_room": {"agents": 8, "skills": 3, "diary": 1},
        "recent_drawers": [],
        "pattern_candidates": [
            {"title": "Scout source linkedin returned 0 results", "body": "x" * 80, "created_at": "2026-04-15T10:30:00+00:00"},
            {"title": "Workday boilerplate filter triggered", "body": "y" * 80, "created_at": "2026-04-10T14:00:00+00:00"},
        ],
        "error": None,
    }


def _basic_skills():
    return [
        {"name": "score_calibration_senior_pm", "success": 23, "fail": 2, "confidence": 0.92},
        {"name": "boilerplate_filter_workday", "success": 18, "fail": 0, "confidence": 0.99},
        {"name": "score_calibration_fintech_early_stage", "success": 12, "fail": 4, "confidence": 0.42},
    ]


def _basic_principles():
    return [
        "Never score a role I don't have a JD for.",
        "Never skip CV Handler grounding before calibrating.",
        "Always emit batch_summary even if batch is empty.",
    ]


def test_render_produces_all_six_sections():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
    )
    headings = [
        "## Operating Stats",
        "## Calibration State",
        "## Learned Patterns",
        "## Skills in Use",
        "## Constitutional Principles",
        "## Nudges Inbox",
    ]
    last_idx = -1
    for h in headings:
        idx = out.find(h)
        assert idx > last_idx, f"heading {h!r} missing or out of order in:\n{out}"
        last_idx = idx


def test_render_operating_stats_format():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
    )
    # Spec lines from my-hermes-agents-shiny-sky.md Part 4
    assert "Runs last" in out
    assert "Work units" in out
    assert "Confidence bands" in out
    assert "Avg latency" in out


def test_render_calibration_state_with_no_data_falls_back():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
        calibration=None,
    )
    assert "not yet established" in out.lower() or "awaiting first critic" in out.lower()


def test_render_learned_patterns_appends_dated_entries():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
    )
    # Each pattern entry must be prefixed with **Pattern (YYYY-MM-DD):**
    assert "**Pattern (" in out
    # The two pattern_candidates from fixture should appear
    assert "linkedin returned 0 results" in out


def test_render_skills_in_use_orders_by_confidence():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
    )
    # Skill with confidence 0.42 must be flagged
    assert "**flagged for review**" in out
    # The flagged skill is fintech_early_stage; must appear after the high-confidence skills
    boilerplate_idx = out.find("boilerplate_filter_workday")
    fintech_idx = out.find("score_calibration_fintech_early_stage")
    assert 0 < boilerplate_idx < fintech_idx


def test_render_constitutional_principles_uses_soul_text():
    principles = ["Sacred rule one.", "Sacred rule two."]
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=principles,
        skills_observed=_basic_skills(),
        generated_at=NOW,
    )
    assert "1. Sacred rule one." in out
    assert "2. Sacred rule two." in out


def test_render_nudges_inbox_uses_checkbox_syntax():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        nudges=["Try dropping reasoning_effort", "Inspect 14d trend"],
        generated_at=NOW,
    )
    assert "- [ ] Try dropping reasoning_effort" in out
    assert "- [ ] Inspect 14d trend" in out


def test_render_includes_metadata_footer():
    out = render(
        agent="scout",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        generated_at=NOW,
        source="bootstrap",
    )
    assert "Generated by Curator at" in out
    assert "bootstrap" in out


def test_render_main_appends_under_separator():
    existing = "# MAIN — Diego notes\n\nOriginal content here.\n"
    out = render(
        agent="main",
        audit_stats=_basic_audit(),
        drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(),
        skills_observed=_basic_skills(),
        existing_content=existing,
        mode="append",
        generated_at=NOW,
    )
    assert out.startswith(existing)
    assert "Curator-Bootstrapped Sections" in out
    assert "## Operating Stats" in out


def test_render_meets_size_floor():
    """Rendered output for a representative-sized input must exceed 5KB.

    Models a real busy agent: 8 patterns surfaced with full bodies, 6
    skills with metadata, 5 nudges, recent drawer listing populated.
    """
    audit = _basic_audit()
    audit["event_type_counts"] = {
        "cron_completed": 20, "cron_failed": 1, "mailbox_message": 25,
        "scout_discovery": 8, "score_batch_summary": 12,
    }
    drawers = {
        "agent": "scout",
        "drawer_count_total": 35,
        "drawers_by_room": {"agents": 14, "skills": 8, "diary": 7, "decisions": 6},
        "recent_drawers": [
            {"title": f"Recent drawer entry {i}: linkedin scrape result with full body", "body": "x" * 60, "created_at": f"2026-04-{15+i:02d}T10:00:00+00:00", "wing": ".openclaw", "room": "agents"}
            for i in range(8)
        ],
        "pattern_candidates": [
            {
                "title": f"Pattern title {i}: scout source linkedin returned 0 results across multiple sessions",
                "body": "Detailed pattern body with rationale and evidence. " * 6,
                "created_at": "2026-04-15T10:00:00+00:00",
                "wing": ".openclaw",
                "room": "agents",
            }
            for i in range(8)
        ],
        "error": None,
    }
    skills = [
        {"name": f"skill_calibration_pattern_{i}", "success": 20 - i, "fail": i, "confidence": 0.9 - i * 0.1}
        for i in range(6)
    ]
    nudges = [
        f"Nudge {i}: try dropping reasoning_effort to medium for clear-cut cases (Critic proposal 2026-04-{20+i})"
        for i in range(5)
    ]
    out = render(
        agent="scout",
        audit_stats=audit,
        drawer_data=drawers,
        constitutional_principles=_basic_principles(),
        skills_observed=skills,
        nudges=nudges,
        generated_at=NOW,
    )
    assert len(out.encode("utf-8")) > 5120, f"size was {len(out.encode('utf-8'))} bytes"


# ---------------------------------------------------------------------------
# Bounded carry-forward (bound MEMORY.md growth) — 2026-07-14
# ---------------------------------------------------------------------------


def test_stable_prefix_keeps_human_head_before_curator_banner():
    content = (
        "# Jaum Memory\n\n## Orchestration Rules\n- keep this\n\n"
        "---\n\n# Curator-Bootstrapped Sections (2026-07-10)\n\n"
        "# MEMORY — main\n\n## Operating Stats\n- drop this\n"
    )
    prefix = _stable_prefix(content)
    assert "# Jaum Memory" in prefix
    assert "- keep this" in prefix
    assert "Curator-Bootstrapped Sections" not in prefix
    assert "## Operating Stats" not in prefix
    # No trailing '---' separator carried over (idempotence).
    assert not prefix.rstrip().endswith("-")


def test_stable_prefix_empty_for_pure_curator_file():
    # Non-main file: stacked empty '## Prior Notes' + a curator banner, no human content.
    content = (
        "## Prior Notes (pre-2026-07-14)\n\n## Prior Notes (pre-2026-07-13)\n\n"
        "# MEMORY — scout\n\n## Operating Stats\n- regenerated nightly\n"
    )
    assert _stable_prefix(content) == ""


def test_render_append_drops_stale_curator_blocks():
    # Existing content already holds a prior curator block; append must NOT carry it forward.
    existing = (
        "# Jaum Memory\n\n- human note\n\n---\n\n"
        "# Curator-Bootstrapped Sections (2026-07-01)\n\n# MEMORY — main\n\n"
        "## Operating Stats\n- STALE BLOCK MARKER\n"
    )
    out = render(
        agent="main", audit_stats=_basic_audit(), drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(), skills_observed=_basic_skills(),
        existing_content=existing, mode="append", generated_at=NOW,
    )
    assert "# Jaum Memory" in out
    assert "- human note" in out
    assert "STALE BLOCK MARKER" not in out          # old block dropped
    assert out.count("## Operating Stats") == 1      # exactly one fresh block


def test_render_append_is_idempotent():
    existing = "# Jaum Memory\n\n- human note\n"
    first = render(
        agent="main", audit_stats=_basic_audit(), drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(), skills_observed=_basic_skills(),
        existing_content=existing, mode="append", generated_at=NOW,
    )
    second = render(
        agent="main", audit_stats=_basic_audit(), drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(), skills_observed=_basic_skills(),
        existing_content=first, mode="append", generated_at=NOW,
    )
    # Re-rendering the previous output must be byte-identical: no stacked block,
    # no accumulating separators, human head intact.
    assert second == first
    assert first.count("## Operating Stats") == 1
    assert "# Jaum Memory" in second and "- human note" in second


def test_render_preserve_with_prior_collapses_to_replace():
    existing = (
        "## Prior Notes (pre-2026-07-14)\n\n# MEMORY — scout\n\n"
        "## Operating Stats\n- STALE\n"
    )
    out = render(
        agent="scout", audit_stats=_basic_audit(), drawer_data=_basic_drawers(),
        constitutional_principles=_basic_principles(), skills_observed=_basic_skills(),
        existing_content=existing, mode="preserve_with_prior", generated_at=NOW,
    )
    assert "STALE" not in out                         # no prior carried forward
    assert "## Prior Notes" not in out                # no wrapper (nothing human to wrap)
    assert out.count("## Operating Stats") == 1
