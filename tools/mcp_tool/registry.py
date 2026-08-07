#!/usr/bin/env python3
"""MCP tool registration, discovery, and registry bookkeeping.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.registry import register_mcp_servers`` continues
to work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _CachedMCPTool,
    _build_utility_schemas,
    _convert_mcp_schema,
    _discover_and_register_server,
    _existing_tool_names,
    _forget_mcp_tool_server,
    _get_lifecycle_seconds,
    _normalize_name_filter,
    _parse_boolish,
    _register_from_cache_sync,
    _register_server_tools,
    _select_utility_schemas,
    _track_mcp_tool_server,
    discover_mcp_tools,
    get_mcp_status,
    get_registered_mcp_server_names,
    has_registered_mcp_tools,
    is_mcp_tool_parallel_safe,
    matches_name_filter,
    mcp_prefixed_tool_name,
    probe_mcp_server_tools,
    refresh_agent_mcp_tools,
    register_mcp_servers,
    sanitize_mcp_name_component,
)

__all__ = [
    "_CachedMCPTool",
    "_build_utility_schemas",
    "_convert_mcp_schema",
    "_discover_and_register_server",
    "_existing_tool_names",
    "_forget_mcp_tool_server",
    "_get_lifecycle_seconds",
    "_normalize_name_filter",
    "_parse_boolish",
    "_register_from_cache_sync",
    "_register_server_tools",
    "_select_utility_schemas",
    "_track_mcp_tool_server",
    "discover_mcp_tools",
    "get_mcp_status",
    "get_registered_mcp_server_names",
    "has_registered_mcp_tools",
    "is_mcp_tool_parallel_safe",
    "matches_name_filter",
    "mcp_prefixed_tool_name",
    "probe_mcp_server_tools",
    "refresh_agent_mcp_tools",
    "register_mcp_servers",
    "sanitize_mcp_name_component",
]
