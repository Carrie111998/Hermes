"""Tests for explicit /refine focus and rollback snapshots."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    spawn_background_review_thread,
)
from run_agent import AIAgent


def _bare_agent():
    agent = MagicMock()
    # Ensure getattr(agent, "_COMBINED_REVIEW_PROMPT", default) hits the default.
    del agent._COMBINED_REVIEW_PROMPT
    del agent._MEMORY_REVIEW_PROMPT
    del agent._SKILL_REVIEW_PROMPT
    return agent


def test_no_focus_prompt_is_byte_identical():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus=None
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus="   "
    )
    assert prompt == _COMBINED_REVIEW_PROMPT


def test_focus_is_appended_to_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True,
        focus="save the deploy workflow as a skill",
    )
    assert prompt.startswith(_COMBINED_REVIEW_PROMPT)
    assert "save the deploy workflow as a skill" in prompt
    assert "explicitly requested" in prompt


def test_focus_works_with_memory_only_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=False, focus="remember my timezone",
    )
    assert prompt.startswith(_MEMORY_REVIEW_PROMPT)
    assert "remember my timezone" in prompt


def test_explicit_refine_snapshots_skills_before_thread_starts():
    """A user-triggered /refine must have a reviewable rollback point."""
    agent = MagicMock()
    thread = MagicMock()
    snapshot_dir = Path("/tmp/.curator_backups/2026-08-11T12-00-00Z")

    with (
        patch(
            "agent.background_review.spawn_background_review_thread",
            return_value=(lambda: None, "prompt"),
        ),
        patch(
            "agent.curator_backup.snapshot_skills",
            return_value=snapshot_dir,
        ) as snapshot_skills,
        patch("run_agent.threading.Thread", return_value=thread),
    ):
        snapshot_id = AIAgent._spawn_background_review(
            agent,
            messages_snapshot=[{"role": "user", "content": "refine this"}],
            review_memory=True,
            review_skills=True,
            focus="capture the workflow",
            snapshot_before_writes=True,
        )

    snapshot_skills.assert_called_once_with(reason="pre-refine")
    thread.start.assert_called_once_with()
    assert snapshot_id == snapshot_dir.name


def test_automatic_review_does_not_create_refinement_snapshot():
    """Routine post-turn reviews stay cheap and byte-compatible."""
    agent = MagicMock()
    thread = MagicMock()

    with (
        patch(
            "agent.background_review.spawn_background_review_thread",
            return_value=(lambda: None, "prompt"),
        ),
        patch("agent.curator_backup.snapshot_skills") as snapshot_skills,
        patch("run_agent.threading.Thread", return_value=thread),
    ):
        snapshot_id = AIAgent._spawn_background_review(
            agent,
            messages_snapshot=[{"role": "user", "content": "ordinary turn"}],
            review_memory=True,
            review_skills=True,
        )

    snapshot_skills.assert_not_called()
    thread.start.assert_called_once_with()
    assert snapshot_id is None


def test_explicit_skill_refine_does_not_start_without_snapshot():
    """Explicit autonomous skill writes fail closed when rollback is unavailable."""
    agent = MagicMock()

    with (
        patch(
            "agent.background_review.spawn_background_review_thread",
            return_value=(lambda: None, "prompt"),
        ),
        patch("agent.curator_backup.snapshot_skills", return_value=None),
        patch("run_agent.threading.Thread") as thread_cls,
    ):
        with pytest.raises(RuntimeError, match="rollback snapshot"):
            AIAgent._spawn_background_review(
                agent,
                messages_snapshot=[{"role": "user", "content": "refine this"}],
                review_memory=True,
                review_skills=True,
                snapshot_before_writes=True,
            )

    thread_cls.assert_not_called()
