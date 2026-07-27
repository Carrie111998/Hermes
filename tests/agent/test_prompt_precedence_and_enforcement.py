"""Precedence must be stated (H-08); inert enforcement must be visible (H-07).

H-08: the stable tier is identity-first, then guidance blocks. Later text reads
as more specific and more actionable, so without an explicit rule a guidance
block silently outranks the constitution it follows — which is how
SKILLS_GUIDANCE came to order unreviewed skill edits against a SOUL requiring
approval. Position alone is not a precedence rule.

H-07: tool_use_enforcement "auto" matches the MODEL NAME against family
substrings. When the model is a router alias the real family is hidden, nothing
matches, and both the enforcement block and the per-family operational guidance
behind it are silently never injected.
"""

from __future__ import annotations

import inspect

import pytest

from agent import prompt_builder as pb
from agent import system_prompt as sp


# ── H-08: the precedence rule exists and is placed correctly ─────────────────

def test_precedence_note_exists_and_names_the_winner():
    note = pb.PROMPT_PRECEDENCE_NOTE
    assert "identity wins" in note, "the note must say which side wins"
    assert "never grants an authorisation" in note, (
        "guidance must not be able to grant authority the identity withholds"
    )


def test_precedence_note_covers_the_categories_that_actually_collided():
    note = pb.PROMPT_PRECEDENCE_NOTE.lower()
    for topic in ("approval", "safety", "destructive", "trustworthy"):
        assert topic in note, f"precedence note does not cover {topic!r}"


def test_precedence_note_requires_disclosure_not_silent_choice():
    """A silently-resolved conflict is how H-06 went unnoticed."""
    assert "say so plainly" in pb.PROMPT_PRECEDENCE_NOTE


def test_note_is_emitted_between_identity_and_guidance():
    src = inspect.getsource(sp.build_system_prompt_parts)
    soul_at = src.index("load_soul_md")
    note_at = src.index("PROMPT_PRECEDENCE_NOTE")
    guidance_at = src.index("HERMES_AGENT_HELP_GUIDANCE")
    assert soul_at < note_at < guidance_at, (
        "the precedence note must sit at the identity/guidance boundary"
    )


def test_note_is_only_emitted_when_a_real_identity_loaded():
    """With no SOUL there is no constitution to outrank, so the note would be
    describing a boundary that does not exist."""
    src = inspect.getsource(sp.build_system_prompt_parts)
    guarded = src[src.index("if _soul_loaded:"):src.index("PROMPT_PRECEDENCE_NOTE")]
    assert "stable_parts.append" not in guarded.split("\n")[0]


def test_note_is_static_so_the_prefix_cache_survives():
    """The stable tier is byte-compared across turns; a dynamic note would
    invalidate the upstream prompt cache on every rebuild."""
    assert pb.PROMPT_PRECEDENCE_NOTE == pb.PROMPT_PRECEDENCE_NOTE
    assert "{" not in pb.PROMPT_PRECEDENCE_NOTE, "no interpolation placeholders"


# ── H-07: auto-detection cannot match a router alias ─────────────────────────

@pytest.mark.parametrize("alias", ["mythos-heavy", "mythos-cheap", "local-fast", "hermes-act"])
def test_router_aliases_match_no_model_family(alias):
    """Documents the gap: these are the real model names in this deployment."""
    assert not [p for p in pb.TOOL_USE_ENFORCEMENT_MODELS if p in alias], (
        f"{alias!r} now matches a family substring — re-check whether the "
        "doctor warning is still correct"
    )


@pytest.mark.parametrize("real", ["gpt-5", "codex-mini", "gemini-2.5", "glm-4.7", "qwen3-max"])
def test_real_family_names_still_match(real):
    assert [p for p in pb.TOOL_USE_ENFORCEMENT_MODELS if p in real]


def test_doctor_reports_when_auto_can_never_fire():
    """The gap is invisible without this; 'auto' looks like it is working."""
    src = (
        inspect.getsource(__import__("hermes_cli.doctor", fromlist=["doctor"]))
    )
    assert "TOOL_USE_ENFORCEMENT_MODELS" in src, (
        "doctor no longer checks whether tool-use enforcement can fire"
    )
    assert "never fires" in src
