from types import SimpleNamespace


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
    }


def test_refresh_tool_execution_bindings_restores_schema_visible_tools():
    from agent.conversation_loop import refresh_tool_execution_bindings

    agent = SimpleNamespace(
        tools=[_tool("terminal"), _tool("read_file"), _tool("write_file")],
        valid_tool_names=set(),
    )

    names = refresh_tool_execution_bindings(agent)

    assert names == {"terminal", "read_file", "write_file"}
    assert agent.valid_tool_names == names
