#!/usr/bin/env python3
"""Dedicated MCP background event loop management.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.loop import _run_on_mcp_loop`` continues to work
after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _LockCookie,
    _acquire_lock_on_fh,
    _ensure_mcp_loop,
    _filter_mcp_children,
    _interrupted_call_result,
    _mcp_loop_exception_handler,
    _run_on_mcp_loop,
    _snapshot_child_pids,
    _try_acquire_mcp_discovery_lock,
    _wrap_with_dashboard_oauth_flow,
    _wrap_with_home_override,
)

__all__ = [
    "_LockCookie",
    "_acquire_lock_on_fh",
    "_ensure_mcp_loop",
    "_filter_mcp_children",
    "_interrupted_call_result",
    "_mcp_loop_exception_handler",
    "_run_on_mcp_loop",
    "_snapshot_child_pids",
    "_try_acquire_mcp_discovery_lock",
    "_wrap_with_dashboard_oauth_flow",
    "_wrap_with_home_override",
]
