"""Behavior contracts for journey node edit/delete (agent.learning_mutations).

Exercises the real on-disk resolution (skills dir + MEMORY.md/USER.md chunking)
against a temp HERMES_HOME, never mocks — the id→file mapping is the whole point.
"""

from __future__ import annotations

import multiprocessing
import os

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


def _exclude_wiki_process(home: str, wiki: str, node_id: str, ready, start, results) -> None:
    os.environ["HERMES_HOME"] = home
    os.environ["WIKI_PATH"] = wiki
    ready.set()
    start.wait()
    from agent import learning_mutations

    results.put(learning_mutations.delete_node(node_id))


def _hold_wiki_exclusion_lock(index: str, acquired, release) -> None:
    from pathlib import Path

    from agent import learning_mutations

    with learning_mutations._wiki_exclusion_lock(Path(index)):
        acquired.set()
        release.wait(10)


@pytest.fixture
def home(monkeypatch):
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
    monkeypatch.setenv("WIKI_PATH", str(wiki))
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


def test_wiki_detail_and_edit_resolve_runtime_page(home):
    detail = lm.node_detail("wiki:project.md")
    assert detail["ok"] and detail["kind"] == "wiki" and detail["label"] == "Project"

    assert lm.edit_node("wiki:project.md", "# Updated\n")["ok"]
    assert (home / "wiki" / "project.md").read_text(encoding="utf-8") == "# Updated\n"


def test_wiki_edit_reports_replace_failure_and_cleans_unique_temp(home, monkeypatch):
    page = home / "wiki" / "project.md"
    sources = []

    def fail_replace(source, destination):
        sources.append((source, destination))
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    result = lm.edit_node("wiki:project.md", "# Updated\n")

    assert not result["ok"]
    assert result["message"] == "replace failed"
    assert page.read_text(encoding="utf-8").endswith("Original.")
    assert len(sources) == 1
    assert sources[0][0].name != "project.md.tmp"
    assert sources[0][1] == page
    assert not list(page.parent.glob("*.tmp"))


def test_delete_wiki_hides_node_without_deleting_file(home):
    page = home / "wiki" / "project.md"
    assert lm.delete_node("wiki:project.md")["ok"]
    assert page.exists()

    from agent.learning_graph import _wiki_cards

    assert not _wiki_cards()


def test_wiki_exclusions_are_scoped_to_the_active_root(home, monkeypatch, tmp_path):
    assert lm.delete_node("wiki:project.md")["ok"]
    other_wiki = tmp_path / "other-wiki"
    other_wiki.mkdir()
    (other_wiki / "project.md").write_text("# Other project\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(other_wiki))

    from agent.learning_graph import _wiki_cards

    assert [card["path"] for card in _wiki_cards()] == ["project.md"]
    assert lm.delete_node("wiki:project.md")["ok"]

    monkeypatch.setenv("WIKI_PATH", str(home / "wiki"))
    assert _wiki_cards() == []


def test_concurrent_wiki_exclusions_preserve_both_pages(home):
    wiki = home / "wiki"
    (wiki / "second.md").write_text("# Second\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    processes = []
    try:
        for node_id in ("wiki:project.md", "wiki:second.md"):
            ready = ctx.Event()
            process = ctx.Process(
                target=_exclude_wiki_process,
                args=(str(home), str(wiki), node_id, ready, start, results),
            )
            process.start()
            processes.append(process)
            assert ready.wait(5)

        start.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert [results.get(timeout=1)["ok"] for _ in processes] == [True, True]
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        results.close()
        results.join_thread()

    from agent.learning_graph import _wiki_cards

    assert _wiki_cards() == []
    assert not list((home / "journey").glob("*.tmp"))


def test_wiki_exclusion_lock_timeout_fails_without_writing(home, monkeypatch):
    index = home / "journey" / "wiki-excluded.json"
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_wiki_exclusion_lock, args=(str(index), acquired, release))
    holder.start()
    try:
        assert acquired.wait(5)
        monkeypatch.setattr(lm, "_WIKI_EXCLUSION_LOCK_TIMEOUT_SECONDS", 0.1)

        result = lm.delete_node("wiki:project.md")

        assert not result["ok"]
        assert result["message"] == "wiki exclusion index is busy — try again"
        assert not index.exists()
        assert not list(index.parent.glob("*.tmp"))
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
    assert holder.exitcode == 0


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
