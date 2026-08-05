from __future__ import annotations

import pytest


pytest.importorskip("mcp")


def test_dashboard_mcp_exposes_only_read_only_page_tools():
    from hermes_cli.dashboard_mcp import create_dashboard_mcp_server

    server = create_dashboard_mcp_server()
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert set(tools) == {"dashboard_pages_list", "dashboard_link_get"}
    assert set(tools["dashboard_link_get"].parameters["properties"]) == {"page_id"}
    assert tools["dashboard_link_get"].parameters["required"] == ["page_id"]
