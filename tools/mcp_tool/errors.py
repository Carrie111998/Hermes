#!/usr/bin/env python3
"""MCP error classification and sanitization helpers.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.errors import InvalidMcpUrlError`` continues to
work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    InvalidMcpUrlError,
    NonMcpEndpointError,
    _classify_mcp_failure,
    _contains_only_cancellation,
    _exc_str,
    _format_connect_error,
    _is_method_not_found_error,
    _sanitize_error,
    _unwrap_exception_group,
)

__all__ = [
    "InvalidMcpUrlError",
    "NonMcpEndpointError",
    "_classify_mcp_failure",
    "_contains_only_cancellation",
    "_exc_str",
    "_format_connect_error",
    "_is_method_not_found_error",
    "_sanitize_error",
    "_unwrap_exception_group",
]
