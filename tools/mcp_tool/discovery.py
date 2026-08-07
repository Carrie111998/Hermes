#!/usr/bin/env python3
"""MCP server discovery, connection, and tool-handler factories.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.discovery import _make_tool_handler`` continues
to work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _connect_server,
    _ensure_lazy_server_connected,
    _filter_suspicious_mcp_servers,
    _get_connected_server_for_call,
    _load_mcp_config,
    _make_check_fn,
    _make_get_prompt_handler,
    _make_list_prompts_handler,
    _make_list_resources_handler,
    _make_read_resource_handler,
    _make_tool_handler,
    _mark_server_call_started,
    _normalize_mcp_input_schema,
    _request_lazy_reconnect,
    _resolve_server_lazy,
)

__all__ = [
    "_connect_server",
    "_ensure_lazy_server_connected",
    "_filter_suspicious_mcp_servers",
    "_get_connected_server_for_call",
    "_load_mcp_config",
    "_make_check_fn",
    "_make_get_prompt_handler",
    "_make_list_prompts_handler",
    "_make_list_resources_handler",
    "_make_read_resource_handler",
    "_make_tool_handler",
    "_mark_server_call_started",
    "_normalize_mcp_input_schema",
    "_request_lazy_reconnect",
    "_resolve_server_lazy",
]
