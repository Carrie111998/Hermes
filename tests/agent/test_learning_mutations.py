"""Behavior contracts for journey node edit/delete (agent.learning_mutations).

Exercises the real on-disk resolution (skills dir + MEMORY.md/USER.md chunking)
against a temp HERMES_HOME, never mocks — the id→file mapping is the whole point.
"""

from __future__ import annotations

import pytest

from agent import learning_mutations as lm
from hermes_constants import get_hermes_home

_SKILL = """---
name: my-skill
description: A test skill.
---

# My Skill

Body.
"""


@pytest.fixture
def home():
    base = get_hermes_home()
    (base / "memories").mkdir(parents=True, exist_ok=True)
    (base / "memories" / "MEMORY.md").write_text("alpha note\nline two\n§\nbeta note", encoding="utf-8")
    (base / "memories" / "USER.md").write_text("user profile note", encoding="utf-8")
    skill = base / "skills" / "my-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return base


def test_parse_node_kind():
    assert lm.parse_node_kind("memory:memory:0") == "memory"
    assert lm.parse_node_kind("memory:profile:3") == "memory"
    assert lm.parse_node_kind("debugging-hermes") == "skill"








def test_edit_memory_replaces_chunk(home):
    assert lm.edit_node("memory:profile:2", "rewritten profile")["ok"]
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8").strip() == "rewritten profile"








def test_skill_detail_returns_skill_md(home):
    d = lm.node_detail("my-skill")
    assert d["ok"] and d["kind"] == "skill"
    assert "name: my-skill" in d["content"]




def test_delete_pinned_skill_refused(home):
    from tools import skill_usage

    skill_usage.set_pinned("my-skill", True)
    res = lm.delete_node("my-skill")
    assert not res["ok"]
    assert "pinned" in res["message"]
    assert (home / "skills" / "my-skill").exists()






def test_memory_writes_match_memory_tool_format(home):
    """A journey mutation must leave the file byte-identical to what the memory
    tool itself writes — same §-join, no trailing-newline drift — so the two
    surfaces never fight over format and indices stay aligned."""
    from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

    assert lm.edit_node("memory:memory:0", "alpha rewritten")["ok"]
    path = home / "memories" / "MEMORY.md"
    entries = MemoryStore._read_file(path)

    assert entries == ["alpha rewritten", "beta note"]
    assert path.read_text(encoding="utf-8") == ENTRY_DELIMITER.join(entries)


def test_delete_memory_archives_evicted_chunk(home):
    """/journey delete on a memory node archives the evicted chunk to
    ARCHIVE.jsonl before the rewrite — the same reversible-delete semantics
    as the memory tool's remove() (#76883)."""
    import json

    res = lm.delete_node("memory:memory:0")
    assert res["ok"]
    assert "ARCHIVE.jsonl" in res["message"]

    archive = home / "memories" / "ARCHIVE.jsonl"
    assert archive.exists()
    records = [
        json.loads(line)
        for line in archive.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["store"] == "memory"
    assert records[0]["action"] == "removed"
    assert records[0]["entry"] == "alpha note\nline two"

    # The delete itself still happened.
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "beta note"


def test_delete_profile_memory_not_archived_by_default(home):
    """Journey profile nodes map to USER.md — archiving must honor the
    ``memory.archive_user: false`` privacy default (#77154 review)."""
    res = lm.delete_node("memory:profile:2")
    assert res["ok"]
    assert "ARCHIVE.jsonl" not in res["message"]
    assert not (home / "memories" / "ARCHIVE.jsonl").exists()
    # The delete itself still happened.
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8").strip() == ""


def test_delete_profile_memory_archived_when_configured(home):
    """With ``memory.archive_user: true``, profile deletes archive as the
    ``user`` store — never mislabeled as ``memory``."""
    import json

    (get_hermes_home() / "config.yaml").write_text(
        "memory:\n  archive_user: true\n", encoding="utf-8"
    )
    res = lm.delete_node("memory:profile:2")
    assert res["ok"]
    assert "ARCHIVE.jsonl" in res["message"]
    archive = home / "memories" / "ARCHIVE.jsonl"
    records = [
        json.loads(line)
        for line in archive.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["store"] == "user"
    assert records[0]["action"] == "removed"
    assert records[0]["entry"] == "user profile note"


def test_edit_memory_archives_superseded(home):
    """/journey edit archives the superseded chunk (reversible edits, same
    ARCHIVE.jsonl) instead of destroying it (#77154 review)."""
    import json

    res = lm.edit_node("memory:memory:0", "alpha rewritten")
    assert res["ok"]
    assert "ARCHIVE.jsonl" in res["message"]
    archive = home / "memories" / "ARCHIVE.jsonl"
    records = [
        json.loads(line)
        for line in archive.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["store"] == "memory"
    assert records[0]["action"] == "superseded"
    assert records[0]["entry"] == "alpha note\nline two"
    # The edit itself landed.
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8").startswith("alpha rewritten")


def test_edit_profile_memory_not_archived_by_default(home):
    """Profile edits honor the privacy gate too — nothing archived by default."""
    res = lm.edit_node("memory:profile:2", "rewritten profile")
    assert res["ok"]
    assert "ARCHIVE.jsonl" not in res["message"]
    assert not (home / "memories" / "ARCHIVE.jsonl").exists()
