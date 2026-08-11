"""Tests for the knowledge plugin backend (plugins/knowledge/dashboard/plugin_api.py).

Exercises the wiki scanner against a temp knowledge dir: frontmatter parsing,
[[wikilink]] extraction, graph construction, read with backlinks, and search.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO_ROOT / "plugins" / "knowledge" / "dashboard" / "plugin_api.py"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    knowledge_dir = tmp_path / "knowledge"
    monkeypatch.setenv("HERMES_KNOWLEDGE_DIR", str(knowledge_dir))
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_knowledge_test", _PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")


def test_empty_graph_seeds_readme(api):
    graph = api.graph()
    assert graph["ok"] is True
    # The seed README is itself a node.
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["id"] == "README.md"
    readme = Path(os.environ["HERMES_KNOWLEDGE_DIR"]) / "README.md"
    assert readme.exists()
    assert "[[wiki-links]]" in readme.read_text("utf-8") or "wiki-links" in readme.read_text("utf-8")


def test_graph_nodes_and_edges(api):
    root = Path(os.environ["HERMES_KNOWLEDGE_DIR"])
    _write(root, "agents.md", "---\ntitle: Agents\ntype: concept\n---\nSee [[tools]] and [[concepts/agents]].")
    _write(root, "tools.md", "---\ntitle: Tools\n---\nUsed by [[agents]].")
    _write(root, "concepts/agents.md", "# Nested\nLinks to [[tools]].")

    graph = api.graph()
    assert len(graph["nodes"]) == 4  # 3 pages + seeded README
    ids = {n["id"] for n in graph["nodes"]}
    assert {"agents.md", "tools.md", "concepts/agents.md"} <= ids
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    # agents.md → tools.md (by name), agents.md → concepts/agents.md (by path)
    assert ("agents.md", "tools.md") in edges
    assert ("agents.md", "concepts/agents.md") in edges
    assert ("tools.md", "agents.md") in edges
    # Concepts page node carries its frontmatter type.
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["agents.md"]["type"] == "concept"


def test_read_page_with_backlinks(api):
    root = Path(os.environ["HERMES_KNOWLEDGE_DIR"])
    _write(root, "a.md", "---\ntitle: A\n---\nLinks [[b]].")
    _write(root, "b.md", "---\ntitle: B\n---\nBody.")

    page = api.read_page(path="b.md")
    assert page["meta"]["title"] == "B"
    assert page["backlinks"] == ["a.md"]

    with pytest.raises(Exception):
        api.read_page(path="../../etc/passwd")
    with pytest.raises(Exception):
        api.read_page(path="missing.md")


def test_search(api):
    root = Path(os.environ["HERMES_KNOWLEDGE_DIR"])
    _write(root, "one.md", "---\ntitle: One\n---\nThe quick brown fox.")
    _write(root, "two.md", "---\ntitle: Two\n---\nNothing here.")

    result = api.search_pages(q="brown")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == "one.md"
    assert result["matches"][0]["line"] == 4  # frontmatter closes on line 3


def test_wikilink_aliases_and_headings(api):
    root = Path(os.environ["HERMES_KNOWLEDGE_DIR"])
    _write(root, "guide.md", "---\ntitle: Guide\n---\nSee [[other-page|Alias]] and [[page#section]].")
    _write(root, "other-page.md", "# Other")
    _write(root, "page.md", "# Page")

    graph = api.graph()
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    # Alias targets clean to the page name; headings strip.
    assert ("guide.md", "other-page.md") in edges
    assert ("guide.md", "page.md") in edges
