"""Tests for the hermes-tools-as-MCP server module surface.

We don't run a live MCP session in unit tests — that requires the codex
subprocess + client + an event loop. These tests pin the static
contract: the module imports, the EXPOSED_TOOLS list is sane, and the
build helper assembles a server when the SDK is present.
"""

from __future__ import annotations

import inspect
from typing import get_args

from agent.transports.hermes_tools_mcp_server import (
    _signature_from_schema,
)


class TestSignatureFromSchema:
    """Test the JSON Schema -> Python signature conversion."""

    def test_simple_required_string_param(self):
        """A required string param becomes str with no default."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        sig, annots = _signature_from_schema(schema)

        assert len(sig.parameters) == 1
        param = sig.parameters["query"]
        assert param.name == "query"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert annots["query"] == str
        assert param.default is inspect.Parameter.empty



    def test_skip_private_params(self):
        """Params starting with '_' are excluded from the signature."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_internal": {"type": "string"},
            },
            "required": ["query", "_internal"],
        }
        sig, annots = _signature_from_schema(schema)

        assert "_internal" not in sig.parameters
        assert "_internal" not in annots
        assert "query" in sig.parameters

    def test_all_json_types(self):
        """All JSON schema types map to correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
            "required": ["s", "i", "n", "b", "a", "o"],
        }
        sig, annots = _signature_from_schema(schema)

        assert annots["s"] == str
        assert annots["i"] == int
        assert annots["n"] == float
        assert annots["b"] == bool
        assert annots["a"] == list
        assert annots["o"] == dict








class TestModuleSurface:
    def test_module_imports_clean(self):
        from agent.transports import hermes_tools_mcp_server as m
        assert callable(m.main)
        assert callable(m._build_server)
        assert isinstance(m.EXPOSED_TOOLS, tuple)
        assert len(m.EXPOSED_TOOLS) > 0

    def test_exposed_tools_are_safe_subset(self):
        """We MUST NOT expose tools codex already has, because codex'
        own builtins are better-integrated with its sandbox + approvals.
        Specifically: no terminal/shell, no read_file/write_file, no
        patch — those are codex's built-in tools."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        forbidden = {
            "terminal", "shell", "read_file", "write_file", "patch",
            "search_files", "process",
        }
        leaked = forbidden & set(EXPOSED_TOOLS)
        assert not leaked, (
            f"these tools must NOT be exposed via the codex callback "
            f"because codex has built-in equivalents: {leaked}"
        )

    def test_build_server_uses_mcp_scoped_tool_loading(self, monkeypatch):
        """MCP startup must not assemble Hermes' full tool catalog."""
        import sys
        import types

        import agent.transports.hermes_tools_mcp_server as m

        calls = []

        def fake_load_mcp_tool_definitions():
            calls.append(True)
            return [{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]

        monkeypatch.setattr(m, "_load_mcp_tool_definitions", fake_load_mcp_tool_definitions)

        class FakeFastMCP:
            def __init__(self, *args, **kwargs):
                self.tools = []

            def add_tool(self, handler, *, name, description):
                self.tools.append((name, description))

        fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
        fake_fastmcp.FastMCP = FakeFastMCP
        fake_server = types.ModuleType("mcp.server")
        fake_server.fastmcp = fake_fastmcp
        fake_mcp = types.ModuleType("mcp")
        fake_mcp.server = fake_server
        monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
        monkeypatch.setitem(sys.modules, "mcp.server", fake_server)
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_fastmcp)

        server = m._build_server()

        assert calls == [True]
        assert server.tools == [("web_search", "Search the web.")]

    def test_build_server_defers_model_tools_until_first_call(self, monkeypatch):
        """MCP build must avoid model_tools and load it only for dispatch."""
        import sys
        import types

        import agent.transports.hermes_tools_mcp_server as m

        monkeypatch.setattr(
            m,
            "_load_mcp_tool_definitions",
            lambda: [{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            raising=False,
        )

        fake_model_tools = types.ModuleType("model_tools")
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        class FakeFastMCP:
            def __init__(self, *args, **kwargs):
                self.tools = []
                self.handlers = {}

            def add_tool(self, handler, *, name, description):
                self.tools.append((name, description))
                self.handlers[name] = handler

        fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
        fake_fastmcp.FastMCP = FakeFastMCP
        fake_server = types.ModuleType("mcp.server")
        fake_server.fastmcp = fake_fastmcp
        fake_mcp = types.ModuleType("mcp")
        fake_mcp.server = fake_server
        monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
        monkeypatch.setitem(sys.modules, "mcp.server", fake_server)
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_fastmcp)

        server = m._build_server()
        assert server.tools == [("web_search", "Search the web.")]
        assert not hasattr(fake_model_tools, "handle_function_call")

        fake_model_tools.handle_function_call = (
            lambda tool_name, args: f"called:{tool_name}:{args}"
        )
        assert server.handlers["web_search"](query="hello") == (
            "called:web_search:{'query': 'hello'}"
        )






class TestMain:
    def test_main_returns_2_when_mcp_unavailable(self, monkeypatch):
        """When the mcp package isn't installed, main() should exit
        cleanly with code 2 and an install hint, not crash."""
        import agent.transports.hermes_tools_mcp_server as m

        def boom_build(*a, **kw):
            raise ImportError("mcp not installed")

        monkeypatch.setattr(m, "_build_server", boom_build)
        rc = m.main(["--verbose"])
        assert rc == 2

    def test_main_handles_keyboard_interrupt(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class FakeServer:
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(m, "_build_server", lambda: FakeServer())
        rc = m.main([])
        assert rc == 0

    def test_main_returns_1_on_runtime_error(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class CrashingServer:
            def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(m, "_build_server", lambda: CrashingServer())
        rc = m.main([])
        assert rc == 1
