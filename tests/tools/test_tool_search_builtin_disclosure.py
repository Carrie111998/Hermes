"""Built-in lazy-disclosure policy and scoped dispatch tests."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(
    name: str,
    description: str = "",
    properties: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


class TestBuiltinConfigParsing:
    def test_builtin_disclosure_defaults_off(self):
        from tools.tool_search import ToolSearchConfig

        cfg = ToolSearchConfig.from_raw(None)
        assert not cfg.builtins.enabled
        assert cfg.builtins.min_schema_tokens == 1500
        assert cfg.builtins.deferred_names == frozenset()

    def test_builtin_groups_resolve_to_reviewed_exact_names(self):
        from tools.tool_search import ToolSearchConfig

        cfg = ToolSearchConfig.from_raw({
            "builtins": {
                "enabled": True,
                "defer": [
                    "browser",
                    "session_search",
                    "delegation",
                    "code_execution",
                    "todo",
                    "vision",
                ],
                "min_schema_tokens": 2500,
            },
        })

        assert cfg.builtins.enabled
        assert cfg.builtins.min_schema_tokens == 2500
        assert {
            "browser_navigate",
            "session_search",
            "delegate_task",
            "execute_code",
            "todo",
            "vision_analyze",
            "image_generate",
            "bfl_flux3_text_to_video",
            "bfl_flux3_get_result",
        } <= cfg.builtins.deferred_names
        assert "web_search" not in cfg.builtins.deferred_names

    def test_unknown_builtin_entry_warns_and_fails_open(self, caplog):
        from tools.tool_search import ToolSearchConfig

        cfg = ToolSearchConfig.from_raw({
            "builtins": {
                "enabled": True,
                "defer": ["browser", "future_dangerous_builtin"],
            },
        })

        assert "future_dangerous_builtin" not in cfg.builtins.deferred_names
        assert "browser_navigate" in cfg.builtins.deferred_names
        assert "future_dangerous_builtin" in caplog.text


class TestBuiltinClassification:
    def test_reviewed_core_tool_defers_only_with_resolved_policy(self):
        from tools.tool_search import ToolSearchConfig, classify_tools

        defs = [_td("browser_navigate"), _td("web_search"), _td("terminal")]
        cfg = ToolSearchConfig.from_raw({
            "builtins": {
                "enabled": True,
                "defer": ["browser"],
                "min_schema_tokens": 0,
            },
        })

        visible, deferrable = classify_tools(defs, builtin_policy=cfg.builtins)
        assert {td["function"]["name"] for td in deferrable} == {"browser_navigate"}
        assert {td["function"]["name"] for td in visible} == {"web_search", "terminal"}

    def test_new_core_tool_stays_eager_when_not_explicitly_reviewed(self, monkeypatch):
        from tools import tool_search

        monkeypatch.setattr(
            tool_search,
            "_core_tool_names",
            lambda: frozenset({"future_essential_core"}),
        )
        cfg = tool_search.ToolSearchConfig.from_raw({
            "builtins": {
                "enabled": True,
                "defer": ["future_essential_core"],
                "min_schema_tokens": 0,
            },
        })
        visible, deferrable = tool_search.classify_tools(
            [_td("future_essential_core")],
            builtin_policy=cfg.builtins,
        )

        assert [td["function"]["name"] for td in visible] == ["future_essential_core"]
        assert deferrable == []


class TestBuiltinAssembly:
    @staticmethod
    def _register_mcp(name):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, "Deferred capability description.")["function"],
            toolset="mcp-tiertest",
        )

    def test_builtin_threshold_applies_only_to_selected_builtin_schemas(self):
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs

        cfg = ToolSearchConfig.from_raw({
            "enabled": "on",
            "builtins": {
                "enabled": True,
                "defer": ["browser"],
                "min_schema_tokens": 10_000,
            },
        })
        defs = [
            _td("browser_navigate", "Small browser schema"),
            _td("terminal", "Run shell"),
        ]

        result = assemble_tool_defs(defs, context_length=200_000, config=cfg)
        assert not result.activated
        assert {td["function"]["name"] for td in result.tool_defs} == {
            "browser_navigate",
            "terminal",
        }

    @pytest.mark.parametrize(
        ("scope", "tool_names"),
        [
            ("hermes-cron narrow job", ["execute_code"]),
            (
                "webhook-safe",
                ["web_search", "web_extract", "vision_analyze", "clarify"],
            ),
            (
                "kanban worker",
                ["kanban_show", "kanban_complete", "kanban_heartbeat"],
            ),
            (
                "delegated leaf",
                ["terminal", "process", "read_file", "patch", "execute_code"],
            ),
        ],
    )
    def test_narrow_scoped_jobs_stay_direct_below_builtin_minimum(
        self, scope, tool_names
    ):
        """Restricted background agents must not pay for an empty bridge."""
        from tools.tool_search import (
            BRIDGE_TOOL_NAMES,
            ToolSearchConfig,
            assemble_tool_defs,
        )

        raw_scoped_defs = [_td(name, f"{scope} action") for name in tool_names]
        cfg = ToolSearchConfig.from_raw({
            "enabled": "on",
            "builtins": {
                "enabled": True,
                "defer": [
                    "browser",
                    "session_search",
                    "delegation",
                    "code_execution",
                    "todo",
                    "vision",
                ],
                "min_schema_tokens": 1500,
            },
        })

        result = assemble_tool_defs(raw_scoped_defs, context_length=200_000, config=cfg)

        assert not result.activated, scope
        assert result.tool_defs == raw_scoped_defs
        assert not (BRIDGE_TOOL_NAMES & {
            td["function"]["name"] for td in result.tool_defs
        })

    def test_builtin_above_threshold_defers_and_catalog_marks_builtin(self):
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs, build_catalog

        cfg = ToolSearchConfig.from_raw({
            "enabled": "on",
            "builtins": {
                "enabled": True,
                "defer": ["browser"],
                "min_schema_tokens": 1,
            },
        })
        defs = [
            _td("browser_navigate", "Navigate a browser"),
            _td("terminal", "Run shell"),
        ]

        result = assemble_tool_defs(defs, context_length=200_000, config=cfg)
        names = {td["function"]["name"] for td in result.tool_defs}
        assert result.activated
        assert "browser_navigate" not in names
        assert "terminal" in names
        assert {"tool_search", "tool_describe", "tool_call"} <= names

        entries = build_catalog([defs[0]])
        assert entries[0].source == "builtin"
        assert entries[0].source_name == "builtin"

    def test_tool_search_off_keeps_selected_builtins_eager(self):
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs

        self._register_mcp("mcp_rollback_probe")
        cfg = ToolSearchConfig.from_raw({
            "enabled": "off",
            "builtins": {
                "enabled": True,
                "defer": ["browser"],
                "min_schema_tokens": 0,
            },
        })
        defs = [
            _td("browser_navigate"),
            _td("terminal"),
            _td("mcp_rollback_probe"),
        ]

        result = assemble_tool_defs(defs, context_length=200_000, config=cfg)
        assert not result.activated
        assert result.tool_defs == defs


