from types import SimpleNamespace

from tools import mcp_tool


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
    }


def test_request_tool_names_restores_schema_visible_tools_without_global_write():
    from agent.conversation_loop import request_tool_names

    request_tools = [_tool("terminal"), _tool("read_file"), _tool("write_file")]
    agent = SimpleNamespace(
        tools=request_tools,
        valid_tool_names=set(),
    )

    names = request_tool_names(request_tools)

    assert names == {"terminal", "read_file", "write_file"}
    assert agent.valid_tool_names == set()


def test_request_tool_names_uses_the_request_snapshot_without_rolling_back_live_state():
    from agent.conversation_loop import request_tool_names

    request_tools = [_tool("terminal"), _tool("read_file")]
    agent = SimpleNamespace(
        tools=[_tool("terminal"), _tool("newly_refreshed_tool")],
        valid_tool_names={"terminal", "newly_refreshed_tool"},
    )

    names = request_tool_names(request_tools)

    assert names == {"terminal", "read_file"}
    assert agent.valid_tool_names == {"terminal", "newly_refreshed_tool"}


def test_inflight_response_detects_real_refresh_without_poisoning_live_snapshot(
    monkeypatch,
):
    from agent.conversation_loop import (
        request_tool_bindings_are_stale,
        request_tool_names,
    )
    from tools.registry import registry

    request_tools = [_tool("terminal"), _tool("read_file")]
    request_generation = registry._generation
    agent = SimpleNamespace(
        tools=request_tools,
        valid_tool_names={"terminal", "read_file"},
        _tool_snapshot_generation=request_generation,
        enabled_toolsets=None,
        disabled_toolsets=None,
    )

    live_tools = [_tool("terminal"), _tool("newly_refreshed_tool")]
    monkeypatch.setattr(
        "model_tools.get_tool_definitions",
        lambda **_kwargs: list(live_tools),
    )
    monkeypatch.setattr(registry, "_generation", request_generation + 1)

    assert mcp_tool.refresh_agent_mcp_tools(agent) == {"newly_refreshed_tool"}
    published_generation = agent._tool_snapshot_generation

    assert request_tool_names(request_tools) == {"terminal", "read_file"}
    assert request_tool_bindings_are_stale(
        agent, request_tools, request_generation
    )
    assert {tool["function"]["name"] for tool in agent.tools} == {
        "terminal",
        "newly_refreshed_tool",
    }
    assert agent.valid_tool_names == {"terminal", "newly_refreshed_tool"}
    assert agent._tool_snapshot_generation == published_generation

    assert mcp_tool.refresh_agent_mcp_tools(agent) == set()
    assert agent.valid_tool_names == {
        tool["function"]["name"] for tool in agent.tools
    }
    assert agent._tool_snapshot_generation == published_generation
