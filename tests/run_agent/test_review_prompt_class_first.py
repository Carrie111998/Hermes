"""Behavior tests for the skill review / combined review prompts.

The review prompts steer the background review agent toward updating the skill
library only for durable, verified, class-level procedures, with a preference
for:
  1. Patching currently-loaded skills first,
  2. Patching existing umbrellas next,
  3. Adding reusable support files under an existing umbrella,
  4. Creating a new class-level umbrella only when nothing else fits.

Personal identity and preferences remain memory signals. Workflow corrections
become skill signals only when they generalize to the whole class of task.

These tests assert behavioral instructions, not the full prompt text.
"""

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# _SKILL_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def test_skill_review_prompt_allows_noop_without_durable_signal():
    """A personal, transient, or unverified session must not force a write."""
    lower = AIAgent._SKILL_REVIEW_PROMPT.lower()
    assert "only when" in lower
    assert "durable, reusable procedure" in lower
    assert "'nothing to save.' is correct" in lower
    assert "missed learning opportunity" not in lower


def test_skill_review_prompt_separates_personal_and_workflow_corrections():
    """Personal preferences stay in memory; general workflow fixes may be skills."""
    lower = AIAgent._SKILL_REVIEW_PROMPT.lower()
    assert "personal identity, preferences, and communication style belong in memory" in lower
    assert "applies to the whole class of task" in lower
    assert "a personal correction alone is not a skill update signal" in lower


def test_skill_review_prompt_prefers_loaded_skills_first():
    """Currently-loaded skills must be the first patch target."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "LOADED" in prompt or "loaded" in prompt, (
        "must mention currently-loaded skills"
    )
    # Must name the mechanisms for detecting loaded skills
    assert "skill_view" in prompt and "/skill" in prompt, (
        "must name skill_view and /skill-name as loaded-skill signals"
    )


def test_skill_review_prompt_has_four_step_preference_order():
    """The 4-step patch/support-file/create ladder must be present."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "PATCH" in prompt
    assert "references/" in prompt or "REFERENCE" in prompt
    assert "CREATE" in prompt
    assert "UMBRELLA" in prompt or "umbrella" in prompt


def test_skill_review_prompt_names_three_support_file_kinds():
    """Support-file step must name references/, templates/, and scripts/."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "references/" in prompt, "must name references/ as a support-file kind"
    assert "templates/" in prompt, "must name templates/ as a support-file kind"
    assert "scripts/" in prompt, "must name scripts/ as a support-file kind"
    # Purpose hints for each kind
    assert "knowledge" in prompt.lower() or "research" in prompt.lower() or "API docs" in prompt, (
        "must mention knowledge-bank / research / API-docs role of references/"
    )
    assert "copied" in prompt.lower() or "starter" in prompt.lower() or "reproduce" in prompt.lower(), (
        "must mention that templates/ are starter files to copy/modify"
    )
    assert "re-runnable" in prompt.lower() or "verification" in prompt.lower() or "probe" in prompt.lower(), (
        "must mention that scripts/ are re-runnable actions"
    )


def test_skill_review_prompt_has_name_veto_for_create():
    """Creating a new skill must be gated behind class-level naming."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "class level" in prompt.lower() or "CLASS-LEVEL" in prompt
    assert "MUST NOT" in prompt or "must not" in prompt, (
        "must have a name-veto clause blocking session-artifact names"
    )


def test_skill_review_prompt_keeps_user_preferences_out_of_skills():
    """Reusable skills must not become user profiles."""
    lower = AIAgent._SKILL_REVIEW_PROMPT.lower()
    assert "skills are shareable procedure, not a user profile" in lower
    assert "personal facts and preferences only through the memory system" in lower


def test_skill_review_prompt_flags_overlap_and_defers_to_curator():
    """Reviewer should not consolidate live; flag overlap for the curator."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "overlap" in prompt.lower()
    assert "curator" in prompt.lower(), "must defer consolidation to the curator"


def test_skill_review_prompt_still_has_opt_out_clause():
    """'Nothing to save.' must remain as a real-but-not-default option."""
    prompt = AIAgent._SKILL_REVIEW_PROMPT
    assert "Nothing to save." in prompt


# ---------------------------------------------------------------------------
# _COMBINED_REVIEW_PROMPT
# ---------------------------------------------------------------------------

def test_combined_review_prompt_has_memory_section():
    """Memory half must retain user facts without copying them into skills."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "**Memory**" in prompt
    assert "memory tool" in prompt
    assert "do not duplicate them into a reusable skill" in prompt


def test_combined_review_prompt_allows_noop_without_durable_skill_signal():
    """Combined review must not force a skill mutation after every session."""
    lower = AIAgent._COMBINED_REVIEW_PROMPT.lower()
    assert "**skills**" in lower
    assert "only when" in lower
    assert "'nothing to save.' is correct" in lower
    assert "missed learning opportunity" not in lower


def test_combined_review_prompt_separates_memory_and_skill_signals():
    """Personal corrections belong to memory unless a procedure generalizes."""
    lower = AIAgent._COMBINED_REVIEW_PROMPT.lower()
    assert "personal identity, preferences, and communication style are memory signals" in lower
    assert "not skill-update signals" in lower
    assert "workflow correction applies to the whole class of task" in lower


def test_combined_review_prompt_prefers_loaded_skills_first():
    """Combined prompt must also prefer loaded skills first."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "LOADED" in prompt or "loaded" in prompt
    assert "skill_view" in prompt and "/skill" in prompt


def test_combined_review_prompt_has_four_step_skill_ladder():
    """Combined prompt must keep the patch/support-file/create ladder on the Skills half."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "PATCH" in prompt
    assert "references/" in prompt or "REFERENCE" in prompt
    assert "CREATE" in prompt
    assert "CLASS-LEVEL" in prompt or "class-level" in prompt or "class level" in prompt.lower()


def test_combined_review_prompt_names_three_support_file_kinds():
    """Combined prompt must also name all three support-file kinds."""
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "references/" in prompt
    assert "templates/" in prompt
    assert "scripts/" in prompt


def test_combined_review_prompt_preserves_opt_out_clause():
    prompt = AIAgent._COMBINED_REVIEW_PROMPT
    assert "Nothing to save." in prompt


# ---------------------------------------------------------------------------
# Anti-pattern guidance — see issue #6051. The reviewer was learning transient
# environment failures (e.g. "browser tools do not work" from a fresh-install
# Playwright miss) as durable skill rules, then citing them against itself for
# weeks after the environment was fixed. Both review prompts must explicitly
# tell the reviewer not to capture environment-dependent or negative-framing
# content as skills.
# ---------------------------------------------------------------------------


def _assert_anti_pattern_guidance(prompt: str, label: str) -> None:
    """Both review prompts must carry the same anti-pattern section."""
    lower = prompt.lower()
    assert "do not capture" in lower, (
        f"{label}: must have an explicit 'Do NOT capture' section"
    )
    # Environment-dependent failures (the #6051 root cause)
    assert any(k in lower for k in ("missing binar", "command not found", "uninstalled", "fresh-install")), (
        f"{label}: must call out environment/setup failures as not-skill-worthy"
    )
    # Negative-framing avoidance
    assert any(k in lower for k in ("negative claim", "do not work", "is broken")), (
        f"{label}: must call out negative-claim phrasings as the failure mode"
    )
    # Positive reframing — "capture the fix, not the failure"
    assert "capture the fix" in lower or "capture the fix " in lower, (
        f"{label}: must redirect tool-failure capture toward the fix, not the constraint"
    )
    # One-off task narratives (#12812 family)
    assert "one-off" in lower, (
        f"{label}: must call out one-off task narratives as not-skill-worthy"
    )


def test_skill_review_prompt_has_anti_pattern_guidance():
    """_SKILL_REVIEW_PROMPT must tell the reviewer NOT to capture transient env failures (#6051)."""
    _assert_anti_pattern_guidance(AIAgent._SKILL_REVIEW_PROMPT, "_SKILL_REVIEW_PROMPT")


def test_combined_review_prompt_has_anti_pattern_guidance():
    """_COMBINED_REVIEW_PROMPT must carry the same guidance — same failure mode applies."""
    _assert_anti_pattern_guidance(AIAgent._COMBINED_REVIEW_PROMPT, "_COMBINED_REVIEW_PROMPT")


# ---------------------------------------------------------------------------
# _MEMORY_REVIEW_PROMPT — unchanged, still memory-focused
# ---------------------------------------------------------------------------

def test_memory_review_prompt_still_focused_on_user_facts():
    """Memory-only review prompt stays focused on user facts — not touched by this change."""
    prompt = AIAgent._MEMORY_REVIEW_PROMPT
    # The memory-only prompt should NOT drift into skill territory
    assert "skills_list" not in prompt
    assert "SURVEY" not in prompt
    assert "memory tool" in prompt
