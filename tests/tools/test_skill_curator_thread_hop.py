"""Regression: the curator's read-before-write mark must survive the thread hop.

Every tool call runs on its own worker thread via
``tools.thread_context.propagate_context_to_thread``, which does a
``contextvars.copy_context()``. When the read-before-write mark lived in a
ContextVar, ``skill_view``'s worker wrote the mark into *its own copy* of the
context and that copy was discarded when the call returned. The later
``skill_manage`` worker started from a fresh copy, never saw the mark, and the
guard refused the write — every time, for every skill.

The user-visible failure was an infinite curator loop: read the skill, try to
patch it, get refused, retry. One observed run burned 5.4M cache-read tokens
over 34 minutes and made the CLI appear frozen.

These tests pin both halves of the invariant:
  * a read in the *same review turn* authorises the write even across threads;
  * a write with no read — or with a read from a *different* turn — is still
    refused, so the fix does not weaken the guard.
"""

import json

import pytest

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread


VALID_SKILL_CONTENT = """---
name: my-skill
description: test skill
---

# My Skill

Original body.
"""


@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + skills dir, as in test_skill_ledger."""
    from agent import skill_utils
    from tools import skill_ledger, skill_manager_tool, skill_usage

    home = tmp_path / "home"
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setattr(skill_ledger, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_usage, "get_hermes_home", lambda: home)
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])
    return {"home": home, "skills": skills_dir}


def _in_worker(fn, *args, **kwargs):
    """Run *fn* the way the tool executor does: own thread, copied context."""
    executor = DaemonThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            propagate_context_to_thread(lambda: fn(*args, **kwargs))
        )
        return future.result(timeout=30)
    finally:
        executor.shutdown(wait=False)


def test_read_mark_survives_thread_hop(curator_env):
    """skill_view on one worker authorises skill_manage on another."""
    from tools.skill_manager_tool import (
        mark_background_review_skill_read,
        skill_manage,
    )
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        begin_review_turn,
        reset_current_write_origin,
        reset_review_turn,
        set_current_write_origin,
    )

    origin = set_current_write_origin(BACKGROUND_REVIEW)
    turn = begin_review_turn("turn-1")
    try:
        assert json.loads(
            skill_manage(action="create", name="my-skill", content=VALID_SKILL_CONTENT)
        )["success"] is True
        skill_md = curator_env["skills"] / "my-skill" / "SKILL.md"

        # Worker A: what skill_view does after returning file content.
        _in_worker(mark_background_review_skill_read, skill_md)

        # Worker B: the write. Before the fix this was refused unconditionally.
        result = json.loads(
            _in_worker(
                skill_manage,
                action="patch",
                name="my-skill",
                old_string="Original body.",
                new_string="Curated body.",
            )
        )
        assert result.get("success") is True, result.get("error")
        assert "Curated body." in skill_md.read_text(encoding="utf-8")
    finally:
        reset_review_turn(turn)
        reset_current_write_origin(origin)


def test_write_without_read_is_still_refused(curator_env):
    """The guard must not be weakened: no read in this turn → no write."""
    from tools.skill_manager_tool import skill_manage
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        begin_review_turn,
        reset_current_write_origin,
        reset_review_turn,
        set_current_write_origin,
    )

    origin = set_current_write_origin(BACKGROUND_REVIEW)
    turn = begin_review_turn("turn-1")
    try:
        assert json.loads(
            skill_manage(action="create", name="my-skill", content=VALID_SKILL_CONTENT)
        )["success"] is True

        result = json.loads(
            _in_worker(
                skill_manage,
                action="patch",
                name="my-skill",
                old_string="Original body.",
                new_string="Unread edit.",
            )
        )
        assert result.get("success") is False
        assert "has not been loaded" in result.get("error", "")
    finally:
        reset_review_turn(turn)
        reset_current_write_origin(origin)


def test_read_does_not_leak_across_review_turns(curator_env):
    """A read in turn 1 must not authorise a write in turn 2."""
    from tools.skill_manager_tool import (
        mark_background_review_skill_read,
        skill_manage,
    )
    from tools.skill_provenance import (
        BACKGROUND_REVIEW,
        begin_review_turn,
        reset_current_write_origin,
        reset_review_turn,
        set_current_write_origin,
    )

    origin = set_current_write_origin(BACKGROUND_REVIEW)
    first = begin_review_turn("turn-1")
    try:
        assert json.loads(
            skill_manage(action="create", name="my-skill", content=VALID_SKILL_CONTENT)
        )["success"] is True
        skill_md = curator_env["skills"] / "my-skill" / "SKILL.md"
        _in_worker(mark_background_review_skill_read, skill_md)
    finally:
        reset_review_turn(first)

    second = begin_review_turn("turn-2")
    try:
        result = json.loads(
            _in_worker(
                skill_manage,
                action="patch",
                name="my-skill",
                old_string="Original body.",
                new_string="Stale-turn edit.",
            )
        )
        assert result.get("success") is False
        assert "has not been loaded" in result.get("error", "")

        # Re-reading inside this turn restores the authorisation.
        _in_worker(
            mark_background_review_skill_read,
            curator_env["skills"] / "my-skill" / "SKILL.md",
        )
        ok = json.loads(
            _in_worker(
                skill_manage,
                action="patch",
                name="my-skill",
                old_string="Original body.",
                new_string="Fresh-turn edit.",
            )
        )
        assert ok.get("success") is True, ok.get("error")
    finally:
        reset_review_turn(second)
        reset_current_write_origin(origin)
