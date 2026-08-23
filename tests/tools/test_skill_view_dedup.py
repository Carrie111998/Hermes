"""Tests for skill_view repeat-view dedup (unchanged-skill stub)."""

import json
import os
import time
from pathlib import Path

import pytest

from tools.skills_tool import (
    _skill_view_with_bump,
    reset_skill_view_dedup,
)


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    d = skills / "demo-dedup-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: demo-dedup-skill\ndescription: Demo skill for dedup tests.\n---\n"
        "# Demo\n\nStep one: run the demo procedure fully.\n"
    )
    refs = d / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\n\nDetailed reference content here.\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_skill_view_dedup()
    return home


def _view(name, file_path=None, task="t-svd"):
    args = {"name": name}
    if file_path:
        args["file_path"] = file_path
    return json.loads(_skill_view_with_bump(args, task_id=task))


class TestSkillViewDedup:
    def test_first_view_returns_full_content(self, skills_home):
        r = _view("demo-dedup-skill")
        assert r["success"] is True
        assert "Step one" in r.get("content", "")

    def test_repeat_view_returns_stub(self, skills_home):
        _view("demo-dedup-skill")
        r2 = _view("demo-dedup-skill")
        assert r2["success"] is True
        assert r2.get("dedup") is True
        assert r2.get("content_returned") is False
        assert "unchanged" in r2["message"]
        assert "content" not in r2

    def test_modified_skill_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        md = skills_home / "skills" / "demo-dedup-skill" / "SKILL.md"
        time.sleep(0.01)
        md.write_text(md.read_text() + "\nStep two: new instruction.\n")
        r2 = _view("demo-dedup-skill")
        assert "Step two" in r2.get("content", "")
        assert r2.get("dedup") is None

    def test_linked_file_dedup_is_independent(self, skills_home):
        _view("demo-dedup-skill")
        # First view of a DIFFERENT file within the skill: full content.
        r = _view("demo-dedup-skill", file_path="references/guide.md")
        assert "Detailed reference" in r.get("content", "")
        # Repeat of that file: stub.
        r2 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert r2.get("dedup") is True

    def test_different_tasks_do_not_share_cache(self, skills_home):
        _view("demo-dedup-skill", task="task-A")
        r = _view("demo-dedup-skill", task="task-B")
        assert "Step one" in r.get("content", "")

    def test_reset_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        reset_skill_view_dedup("t-svd")
        r2 = _view("demo-dedup-skill")
        assert "Step one" in r2.get("content", "")

    def test_no_task_id_never_dedups(self, skills_home):
        args = {"name": "demo-dedup-skill"}
        r1 = json.loads(_skill_view_with_bump(args, task_id=None))
        r2 = json.loads(_skill_view_with_bump(args, task_id=None))
        assert "Step one" in r2.get("content", "")

    def test_compression_hook_importable(self):
        # conversation_compression imports this lazily; keep the seam stable.
        from tools.skills_tool import reset_skill_view_dedup as f
        f(None)


class TestSkillViewDedupCrossTurn:
    """Regression: normal user turns get a fresh task_id UUID every turn (see
    agent/turn_context.py effective_task_id = task_id or uuid4()), so the dedup
    cache keyed on task_id never fires across turns. Bucket by session_id so a
    repeat skill_view in the SAME conversation is deduped even when task_id
    differs between turns.
    """

    def test_same_session_different_task_id_dedups(self, skills_home):
        # Simulates two turns of the same conversation: session_id stable,
        # task_id re-generated each turn (the real-world case).
        args = {"name": "demo-dedup-skill"}
        r1 = json.loads(_skill_view_with_bump(args, task_id="turn-1", session_id="sess-1"))
        r2 = json.loads(_skill_view_with_bump(args, task_id="turn-2", session_id="sess-1"))
        assert r2.get("dedup") is True, "same session + different task_id should dedup"

    def test_different_sessions_stay_isolated(self, skills_home):
        args = {"name": "demo-dedup-skill"}
        r1 = json.loads(_skill_view_with_bump(args, task_id="turn-1", session_id="sess-A"))
        r2 = json.loads(_skill_view_with_bump(args, task_id="turn-1", session_id="sess-B"))
        assert "Step one" in r2.get("content", ""), "different sessions must not share cache"

    def test_session_id_reset_clears_bucket(self, skills_home):
        args = {"name": "demo-dedup-skill"}
        _skill_view_with_bump(args, task_id="t-1", session_id="sess-X")
        reset_skill_view_dedup("sess-X")
        r2 = json.loads(_skill_view_with_bump(args, task_id="t-2", session_id="sess-X"))
        assert "Step one" in r2.get("content", ""), "reset by session_id must clear the bucket"
