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


class TestDedupStubMarksReviewRead:
    """#95976: the stub must satisfy the background review read-before-write guard.

    The guard's read marks are per-review-context (reset each turn), the dedup
    cache is per-task — so a turn-N re-view stubs out without re-marking and
    every fork patch is refused. The stub path now marks the verified-unchanged
    source as read, so the guard sees the current content as loaded.
    """

    def test_stub_satisfies_read_before_write_guard(self, skills_home, monkeypatch):
        from tools import skill_manager_tool as smt

        monkeypatch.setattr(
            "tools.skill_provenance.is_background_review", lambda: True
        )
        smt._reset_background_review_read_marks()

        md = skills_home / "skills" / "demo-dedup-skill" / "SKILL.md"

        # Turn 1: full view marks the read; simulate the next review turn by
        # resetting the per-context marks while the task-level dedup cache
        # survives.
        r1 = _view("demo-dedup-skill")
        assert "Step one" in r1.get("content", "")
        smt._reset_background_review_read_marks()
        assert not smt._background_review_has_read(md)

        # Turn 2: the repeat view returns the stub — and must re-mark, or the
        # fork's patch is refused.
        r2 = _view("demo-dedup-skill")
        assert r2.get("dedup") is True
        assert smt._background_review_has_read(md)

        # The guard that refused every fork patch now passes.
        guard = smt._background_review_read_before_write_guard(
            "demo-dedup-skill", md, "patch", "SKILL.md"
        )
        assert guard is None

    def test_stub_marks_supporting_file_too(self, skills_home, monkeypatch):
        from tools import skill_manager_tool as smt

        monkeypatch.setattr(
            "tools.skill_provenance.is_background_review", lambda: True
        )
        smt._reset_background_review_read_marks()

        guide = skills_home / "skills" / "demo-dedup-skill" / "references" / "guide.md"

        r1 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert "Guide" in r1.get("content", "")
        smt._reset_background_review_read_marks()

        r2 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert r2.get("dedup") is True
        assert smt._background_review_has_read(guide)
