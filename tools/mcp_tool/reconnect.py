#!/usr/bin/env python3
"""MCP server reconnect / auth-recovery helpers.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.reconnect import reconnect_mcp_server`` continues
to work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _bump_server_error,
    _clear_connect_failure,
    _connect_cooldown_active,
    _get_auth_error_types,
    _handle_auth_error_and_retry,
    _handle_session_expired_and_retry,
    _is_auth_error,
    _is_session_expired_error,
    _record_connect_failure,
    _reset_server_error,
    _signal_reconnect,
    _signal_reconnect_and_wait,
    _wait_for_server_session_ready,
    reconnect_mcp_server,
)

__all__ = [
    "_bump_server_error",
    "_clear_connect_failure",
    "_connect_cooldown_active",
    "_get_auth_error_types",
    "_handle_auth_error_and_retry",
    "_handle_session_expired_and_retry",
    "_is_auth_error",
    "_is_session_expired_error",
    "_record_connect_failure",
    "_reset_server_error",
    "_signal_reconnect",
    "_signal_reconnect_and_wait",
    "_wait_for_server_session_ready",
    "reconnect_mcp_server",
]
