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
    wiki = base / "wiki"
    wiki.mkdir()
    (wiki / "project.md").write_text("---\ntitle: Project\n---\n\nOriginal.", encoding="utf-8")
    (base / "config.yaml").write_text(
        f"skills:\n  config:\n    wiki:\n      path: {wiki.as_posix()}\n",
        encoding="utf-8",
    )
    return base


def test_parse_node_kind():
    assert lm.parse_node_kind("memory:memory:0") == "memory"
    assert lm.parse_node_kind("memory:profile:3") == "memory"
    assert lm.parse_node_kind("wiki:project.md") == "wiki"
    assert lm.parse_node_kind("debugging-hermes") == "skill"








def test_edit_memory_replaces_chunk(home):
    assert lm.edit_node("memory:profile:2", "rewritten profile")["ok"]
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8").strip() == "rewritten profile"








def test_skill_detail_returns_skill_md(home):
    d = lm.node_detail("my-skill")
    assert d["ok"] and d["kind"] == "skill"
    assert "name: my-skill" in d["content"]


def test_wiki_detail_and_edit_resolve_configured_page(home):
    detail = lm.node_detail("wiki:project.md")
    assert detail["ok"] and detail["kind"] == "wiki" and detail["label"] == "Project"

    assert lm.edit_node("wiki:project.md", "# Updated\n")["ok"]
    assert (home / "wiki" / "project.md").read_text(encoding="utf-8") == "# Updated\n"


def test_delete_wiki_hides_node_without_deleting_file(home):
    page = home / "wiki" / "project.md"
    assert lm.delete_node("wiki:project.md")["ok"]
    assert page.exists()

    from agent.learning_graph import _wiki_cards

    assert not _wiki_cards()


def test_wiki_node_rejects_path_traversal(home):
    result = lm.node_detail("wiki:../memories/MEMORY.md")
    assert not result["ok"]
    assert "bad wiki node id" in result["message"]




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
