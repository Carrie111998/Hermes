"""Tests for cached xAI tool-schema sanitization."""

from types import SimpleNamespace

from agent.chat_completion_helpers import _sanitize_xai_tool_schemas


def _tool(name="tool", *, pattern="^x$", enum=None):
    parameter = {"type": "string", "pattern": pattern}
    if enum is not None:
        parameter["enum"] = enum
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {"value": parameter}},
        },
    }


def test_xai_schema_sanitization_is_cached_until_tool_snapshot_changes(monkeypatch):
    tools = [_tool(enum=["a/b", "safe"])]
    agent = SimpleNamespace(_tool_snapshot_generation=7)
    calls = 0

    from tools import schema_sanitizer
    original_pattern = schema_sanitizer.strip_pattern_and_format

    def counted_pattern(value):
        nonlocal calls
        calls += 1
        return original_pattern(value)

    monkeypatch.setattr(schema_sanitizer, "strip_pattern_and_format", counted_pattern)

    first = _sanitize_xai_tool_schemas(agent, tools)
    second = _sanitize_xai_tool_schemas(agent, tools)

    assert first == second
    assert first is second
    assert calls == 1
    assert first[0]["function"]["parameters"]["properties"]["value"] == {
        "type": "string"
    }

    tools.append(_tool("new_tool"))
    third = _sanitize_xai_tool_schemas(agent, tools)

    assert third is not first
    assert len(third) == 2
    assert calls == 2


def test_xai_schema_cache_is_scoped_to_tool_snapshot_generation():
    tools = [_tool()]
    agent = SimpleNamespace(_tool_snapshot_generation=1)

    first = _sanitize_xai_tool_schemas(agent, tools)
    agent._tool_snapshot_generation = 2
    second = _sanitize_xai_tool_schemas(agent, tools)

    assert second == first
    assert second is not first
