from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.restricted_knowledge_mcp import (
    KnowledgeError,
    TOOL_NAMES,
    knowledge_list,
    knowledge_read,
    knowledge_search,
    load_roots,
    resolve_path,
)


@pytest.fixture()
def knowledge(tmp_path: Path) -> tuple[dict[str, Path], Path]:
    root = tmp_path / "guide"
    root.mkdir()
    (root / "README.md").write_text("# Guide\nRoutine play\nExpected impact: +4\n")
    (root / "nested").mkdir()
    (root / "nested" / "plan.md").write_text("Stop condition: conversion falls\n")
    (root / ".secret").write_text("hidden")
    roots = load_roots(json.dumps({"guide": str(root)}))
    return roots, root


def test_tool_surface_is_read_only():
    assert TOOL_NAMES == (
        "knowledge_roots",
        "knowledge_list",
        "knowledge_read",
        "knowledge_search",
    )


def test_roots_require_distinct_absolute_non_root_directories(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(KnowledgeError):
        load_roots("{}")
    with pytest.raises(KnowledgeError):
        load_roots(json.dumps({"Guide": str(root)}))
    with pytest.raises(KnowledgeError):
        load_roots(json.dumps({"guide": "relative"}))
    with pytest.raises(KnowledgeError):
        load_roots(json.dumps({"guide": "/"}))
    with pytest.raises(KnowledgeError):
        load_roots(json.dumps({"a": str(root), "b": str(root)}))


def test_resolver_rejects_absolute_parent_hidden_and_symlink_escape(knowledge, tmp_path: Path):
    roots, root = knowledge
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    (root / "escape.md").symlink_to(outside)
    for bad in ("/etc/passwd", "../outside.md", ".secret", "escape.md"):
        with pytest.raises(KnowledgeError):
            resolve_path(roots, "guide", bad)


def test_read_is_bounded_and_line_numbered(knowledge):
    roots, _ = knowledge
    result = knowledge_read(roots, "guide", "README.md", start_line=2, max_lines=2)
    assert result["excerpt"] == "2|Routine play\n3|Expected impact: +4"
    assert result["untrusted_reference_content"] is True
    with pytest.raises(KnowledgeError):
        knowledge_read(roots, "guide", "README.md", max_lines=401)


def test_list_hides_dotfiles_and_symlinks(knowledge, tmp_path: Path):
    roots, root = knowledge
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (root / "escape.md").symlink_to(outside)
    result = knowledge_list(roots, "guide", max_depth=3)
    paths = {entry["path"] for entry in result["entries"]}
    assert paths == {"README.md", "nested/plan.md"}


def test_search_is_bounded_case_insensitive_and_confined(knowledge):
    roots, _ = knowledge
    result = knowledge_search(roots, "guide", "EXPECTED IMPACT", limit=10)
    assert result["results"] == [{
        "path": "README.md",
        "line_number": 3,
        "line": "Expected impact: +4",
    }]
    with pytest.raises(KnowledgeError):
        knowledge_search(roots, "guide", "x")
