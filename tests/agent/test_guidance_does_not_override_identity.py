"""Injected guidance must not contradict the identity slot it follows.

The stable tier is assembled identity-first, then guidance blocks
(system_prompt.build_system_prompt_parts). Anything appended after SOUL is read
as the more specific, more actionable instruction, so a guidance block that
tells the model to act WITHOUT approval silently overrides a profile that
requires it — and there is no runtime gate behind most of these.

H-06 was exactly that: SKILLS_GUIDANCE said "patch it immediately ... don't
wait to be asked", negating a SOUL that classifies skills as procedural memory
requiring a gated change path.
"""

from __future__ import annotations

import re

import pytest

from agent import prompt_builder as pb

# Phrasings that instruct the model to bypass asking. Any of these in an
# injected block outranks a profile rule that requires approval.
_BYPASS_PATTERNS = [
    r"don'?t wait to be asked",
    r"without (?:asking|approval|permission)",
    r"no need to ask",
    r"do not ask (?:the user|first)",
]

_GUIDANCE_BLOCKS = [
    "SKILLS_GUIDANCE",
    "MEMORY_GUIDANCE",
    "TASK_COMPLETION_GUIDANCE",
    "PARALLEL_TOOL_CALL_GUIDANCE",
    "TOOL_USE_ENFORCEMENT_GUIDANCE",
    "SESSION_SEARCH_GUIDANCE",
    "STEER_CHANNEL_NOTE",
]


@pytest.mark.parametrize("name", _GUIDANCE_BLOCKS)
def test_no_guidance_block_instructs_bypassing_approval(name):
    text = getattr(pb, name, None)
    if not isinstance(text, str):
        pytest.skip(f"{name} is not a string constant in this version")
    for pattern in _BYPASS_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        assert not match, (
            f"{name} instructs the model to skip asking ({match.group(0)!r}). "
            "Injected guidance is read after the identity slot and will "
            "override a profile that requires approval, with no gate behind it."
        )


def test_skills_guidance_still_asks_for_maintenance():
    """The fix must not discard the intent — unmaintained skills are a liability."""
    assert "skill_manage" in pb.SKILLS_GUIDANCE
    assert "liabilities" in pb.SKILLS_GUIDANCE


def test_skills_guidance_defers_to_project_discipline():
    lowered = pb.SKILLS_GUIDANCE.lower()
    assert "discipline" in lowered or "approval" in lowered, (
        "guidance should route skill edits through the project's own review "
        "rules rather than mandating an immediate unreviewed patch"
    )


def test_identity_is_assembled_before_guidance():
    """If guidance ever precedes identity, every one of these blocks outranks
    the constitution by position as well as by specificity."""
    import inspect

    from agent import system_prompt as sp

    src = inspect.getsource(sp.build_system_prompt_parts)
    assert src.index("load_soul_md") < src.index("HERMES_AGENT_HELP_GUIDANCE")
