#!/usr/bin/env python3
"""The per-server MCP connection task.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.server_task import MCPServerTask`` continues to
work after the module-to-subpackage refactor.
"""

from tools.mcp_tool import MCPServerTask  # noqa: F401

__all__ = ["MCPServerTask"]
