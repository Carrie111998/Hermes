"""CLI registration smoke tests."""

from __future__ import annotations

import argparse

from plugins.semantic_graph.cli import register_cli
from plugins.semantic_graph.config import SemanticGraphConfig
from plugins.semantic_graph.runtime import SemanticGraphRuntime


def test_cli_status_and_no_model_purge_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = SemanticGraphRuntime(config=SemanticGraphConfig(db_subdir="semantic-graph"))

    class Ctx:
        def __init__(self):
            self.cmds = []

        def register_cli_command(self, **kwargs):
            self.cmds.append(kwargs)

    ctx = Ctx()
    register_cli(ctx, rt)
    assert ctx.cmds[0]["name"] == "semantic-graph"
    parser = argparse.ArgumentParser()
    ctx.cmds[0]["setup_fn"](parser)
    ns = parser.parse_args(["status"])
    assert ctx.cmds[0]["handler_fn"](ns) == 0
    # Model-facing tools must not include purge — checked via schemas in registration tests.
    from plugins.semantic_graph.schemas import TOOL_NAMES

    assert "semantic_graph_purge" not in TOOL_NAMES
