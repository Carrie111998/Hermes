"""MCP toolsets must survive a narrowed ``enabled_toolsets`` allowlist.

MCP toolsets are registered dynamically as ``mcp-<server>`` only once a server
connects, so a caller building a static allowlist (a cron job, platform_toolsets,
an ACP session) cannot name them and used to lose every MCP tool silently.
"""

import model_tools
from tools.registry import ToolRegistry

MCP_TOOL = "mcp__dynserver__ping"
MCP_TOOLSET = "mcp-dynserver"


def _dummy_handler(args, **kwargs):
    return "{}"


def _registry_with_mcp_server():
    reg = ToolRegistry()
    reg.register(
        name=MCP_TOOL,
        toolset=MCP_TOOLSET,
        schema={
            "name": MCP_TOOL,
            "description": "Ping",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_dummy_handler,
    )
    reg.register_toolset_alias("dynserver", MCP_TOOLSET)
    return reg


def _install(monkeypatch, reg, *, delegated_child=False):
    # toolsets.py resolves aliases through tools.registry.registry; model_tools
    # holds its own module-level reference for schema lookup. Patch both.
    monkeypatch.setattr("tools.registry.registry", reg)
    monkeypatch.setattr(model_tools, "registry", reg)
    monkeypatch.setattr(
        model_tools, "_is_delegated_child_context", lambda: delegated_child
    )


def _tool_names(defs):
    return {d["function"]["name"] for d in defs}


def _defs(**kwargs):
    # quiet_mode=False bypasses the memoization path, which keys off the real
    # registry's generation counter. skip_tool_search_assembly=True returns the
    # raw catalog: with tool_search active, MCP tools are otherwise collapsed
    # behind tool_search/tool_describe/tool_call and never appear by name.
    return model_tools.get_tool_definitions(
        quiet_mode=False, skip_tool_search_assembly=True, **kwargs
    )


class TestPreserveMcpToolsets:
    def test_narrowed_allowlist_keeps_mcp_tools(self, monkeypatch):
        """The regression: ['web'] must not silently drop every MCP tool."""
        _install(monkeypatch, _registry_with_mcp_server())
        assert MCP_TOOL in _tool_names(_defs(enabled_toolsets=["web"]))

    def test_opt_out_gives_a_literal_allowlist(self, monkeypatch):
        _install(monkeypatch, _registry_with_mcp_server())
        names = _tool_names(
            _defs(enabled_toolsets=["web"], preserve_mcp_toolsets=False)
        )
        assert MCP_TOOL not in names

    def test_explicit_disable_still_wins(self, monkeypatch):
        """disabled_toolsets is a deliberate exclusion, not an oversight."""
        _install(monkeypatch, _registry_with_mcp_server())
        names = _tool_names(
            _defs(enabled_toolsets=["web"], disabled_toolsets=[MCP_TOOLSET])
        )
        assert MCP_TOOL not in names

    def test_delegated_child_is_not_widened(self, monkeypatch):
        """delegate_tool already re-adds only the *parent's* MCP toolsets;
        widening here would let a child reach servers its parent cannot."""
        _install(monkeypatch, _registry_with_mcp_server(), delegated_child=True)
        assert MCP_TOOL not in _tool_names(_defs(enabled_toolsets=["web"]))

    def test_unrestricted_caller_is_unchanged(self, monkeypatch):
        """enabled_toolsets=None already loads everything."""
        _install(monkeypatch, _registry_with_mcp_server())
        assert MCP_TOOL in _tool_names(_defs(enabled_toolsets=None))
