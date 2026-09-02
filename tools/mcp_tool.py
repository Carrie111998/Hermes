"""Compatibility facade for the MCP tool implementation.

The implementation is loaded from indexed, size-bounded source shards into
this module's namespace. Existing function imports and monkeypatch targets therefore remain stable at
``tools.mcp_tool``. Exported classes retain their defining shard module as an
introspection source while remaining available from this compatibility facade.
"""

from tools.mcp_tool_shards import install as _install_mcp_tool_shards

_install_mcp_tool_shards(globals())
del _install_mcp_tool_shards
