"""Registration tests for the semantic-graph plugin."""

from __future__ import annotations

import importlib
from pathlib import Path

EXPECTED_TOOLS = [
    "semantic_graph_status",
    "semantic_graph_begin_run",
    "semantic_graph_ingest",
    "semantic_graph_submit_fragment",
    "semantic_graph_search",
    "semantic_graph_get",
    "semantic_graph_finalize",
    "semantic_graph_evaluate_output",
    "semantic_graph_feedback",
    "semantic_graph_export",
]

EXPECTED_HOOKS = [
    "pre_llm_call",
    "post_llm_call",
    "post_tool_call",
    "subagent_start",
    "subagent_stop",
    "on_session_finalize",
]


def test_import_does_not_create_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    importlib.invalidate_caches()
    import plugins.semantic_graph  # noqa: F401

    db = home / "semantic-graph" / "semantic_graph.db"
    assert not db.exists()


def test_plugin_yaml_manifest():
    yaml_path = (
        Path(__file__).resolve().parents[2] / "plugins" / "semantic_graph" / "plugin.yaml"
    )
    text = yaml_path.read_text(encoding="utf-8")
    assert "name: semantic-graph" in text
    assert 'description: "Typed provenance graph for AI memory, evidence, and outputs."' in text
    for tool in EXPECTED_TOOLS:
        assert tool in text
    for hook in EXPECTED_HOOKS:
        assert hook in text
    assert "semantic_graph_purge" not in text


def test_register_tools_and_hooks(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from plugins.semantic_graph import register

    class _Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = []
            self.cli = []
            self.llm = None

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, name, cb):
            self.hooks.append(name)

        def register_cli_command(self, **kwargs):
            self.cli.append(kwargs)

    ctx = _Ctx()
    register(ctx)

    names = [t["name"] for t in ctx.tools]
    assert names == EXPECTED_TOOLS
    assert all(t["toolset"] == "semantic_graph" for t in ctx.tools)
    assert ctx.hooks == EXPECTED_HOOKS
    assert ctx.cli and ctx.cli[0]["name"] == "semantic-graph"
    assert "semantic_graph_purge" not in names
