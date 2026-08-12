"""Behavior contracts for the learning-graph assembler.

Asserts invariants (edges resolve to real nodes, clusters cover every node,
memory cards are represented consistently), never a snapshot of the live skill
catalog — that catalog grows every release and a count assertion would be a
change-detector.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent import learning_graph
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _node(name: str, category: str, related=None):
    n = learning_graph.SkillNode(name=name, category=category)
    n.related = list(related or [])
    return n




def test_density_stats_count_isolated_nodes():
    nodes = {
        "a": _node("a", "x", related=["b"]),
        "b": _node("b", "x", related=["a"]),
        "c": _node("c", "y"),
    }
    stats = learning_graph.density_stats(nodes, learning_graph.build_edges(nodes))

    assert stats["nodes"] == 3
    assert stats["linked_nodes"] == 2
    assert stats["isolated_pct"] == round(100 / 3, 1)




def test_memory_is_cards_split_on_separator(tmp_path):
    home = tmp_path / ".hermes"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text(
        "Project uses pytest with xdist\n§\nUser prefers concise responses",
        encoding="utf-8",
    )
    token = set_hermes_home_override(home)
    try:
        graph = learning_graph.build_learning_graph()
    finally:
        reset_hermes_home_override(token)

    titles = [c["title"] for c in graph["memory"]]
    assert "Project uses pytest with xdist" in titles
    assert "User prefers concise responses" in titles
    # Memory cards remain typed cards and also appear as memory-kind nodes.
    assert all(c["source"] in {"memory", "profile"} for c in graph["memory"])
    assert all("timestamp" in c for c in graph["memory"])
    assert any(n["kind"] == "memory" for n in graph["nodes"])


def test_wiki_pages_are_first_class_nodes_with_real_dates_and_edges(tmp_path):
    home = tmp_path / ".hermes"
    wiki = tmp_path / "wiki"
    page = wiki / "projects" / "launch.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntitle: Launch Notes\ndate: 2024-02-03\nrelated_skills: [release-workflow]\n---\n\nLaunch checklist and supplier notes.",
        encoding="utf-8",
    )
    home.mkdir()
    (home / "config.yaml").write_text(
        f"skills:\n  config:\n    wiki:\n      path: {wiki.as_posix()}\n",
        encoding="utf-8",
    )
    token = set_hermes_home_override(home)
    try:
        graph = learning_graph.build_learning_graph()
    finally:
        reset_hermes_home_override(token)

    node = next(n for n in graph["nodes"] if n["id"] == "wiki:projects/launch.md")
    assert node["kind"] == "wiki"
    assert node["label"] == "Launch Notes"
    assert node["timestamp"] == int(datetime(2024, 2, 3, tzinfo=timezone.utc).timestamp())
    assert graph["wiki"][0]["path"] == "projects/launch.md"
    assert graph["stats"]["wiki_nodes"] == 1

    skill = learning_graph.SkillNode(name="release-workflow", category="dev")
    edges = learning_graph._wiki_skill_edges(graph["wiki"], [skill])
    assert ("wiki:projects/launch.md", "release-workflow") in edges


def test_journey_can_opt_out_of_wiki_nodes(tmp_path):
    home = tmp_path / ".hermes"
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# Hidden", encoding="utf-8")
    home.mkdir()
    (home / "config.yaml").write_text(
        f"journey:\n  include_wiki: false\nskills:\n  config:\n    wiki:\n      path: {wiki.as_posix()}\n",
        encoding="utf-8",
    )
    token = set_hermes_home_override(home)
    try:
        graph = learning_graph.build_learning_graph()
    finally:
        reset_hermes_home_override(token)

    assert not any(n["kind"] == "wiki" for n in graph["nodes"])






def test_full_payload_shape_and_edge_integrity(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        graph = learning_graph.build_learning_graph()
    finally:
        reset_hermes_home_override(token)

    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    # Every node's category appears in the cluster list.
    cluster_cats = {c["category"] for c in graph["clusters"]}
    assert all(n["category"] in cluster_cats for n in graph["nodes"])
    skill_nodes = [n for n in graph["nodes"] if n["kind"] == "skill"]
    assert graph["stats"]["nodes"] == len(skill_nodes)
    assert graph["stats"]["memory_nodes"] == len(graph["memory"])
    assert all("timestamp" in n for n in graph["nodes"])
