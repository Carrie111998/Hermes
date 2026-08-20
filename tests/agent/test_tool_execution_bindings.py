from types import SimpleNamespace


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
    }


def test_refresh_tool_execution_bindings_restores_schema_visible_tools():
    from agent.conversation_loop import refresh_tool_execution_bindings

    request_tools = [_tool("terminal"), _tool("read_file"), _tool("write_file")]
    agent = SimpleNamespace(
        tools=request_tools,
        valid_tool_names=set(),
    )

    names = refresh_tool_execution_bindings(agent, request_tools)

    assert names == {"terminal", "read_file", "write_file"}
    assert agent.valid_tool_names == names


def test_refresh_tool_execution_bindings_uses_the_request_snapshot():
    from agent.conversation_loop import refresh_tool_execution_bindings

    request_tools = [_tool("terminal"), _tool("read_file")]
    agent = SimpleNamespace(
        tools=[_tool("terminal"), _tool("newly_refreshed_tool")],
        valid_tool_names={"terminal", "newly_refreshed_tool"},
    )

    names = refresh_tool_execution_bindings(agent, request_tools)

    assert names == {"terminal", "read_file"}
    assert agent.valid_tool_names == names
