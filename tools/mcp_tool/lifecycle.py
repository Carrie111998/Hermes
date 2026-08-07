#!/usr/bin/env python3
"""MCP server lifecycle: shutdown, orphan reaping, loop teardown.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.lifecycle import shutdown_mcp_servers`` continues
to work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _drain_and_stop_mcp_loop,
    _drain_mcp_loop_tasks,
    _kill_orphaned_mcp_children,
    _reinject_post_build_tools,
    _stop_mcp_loop,
    _stop_mcp_loop_if_idle,
    shutdown_mcp_servers,
)

__all__ = [
    "_drain_and_stop_mcp_loop",
    "_drain_mcp_loop_tasks",
    "_kill_orphaned_mcp_children",
    "_reinject_post_build_tools",
    "_stop_mcp_loop",
    "_stop_mcp_loop_if_idle",
    "shutdown_mcp_servers",
]
