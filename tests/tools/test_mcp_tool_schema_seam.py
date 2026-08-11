"""Runtime seam and behavior checks for the extracted MCP schema helpers."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import get_type_hints

from tools import mcp_tool, mcp_tool_schema


_MOVED_NAMES = (
    "MCP_TOOL_NAME_PREFIX",
    "_MCP_NAME_DELIM",
    "_build_utility_schemas",
    "_convert_mcp_schema",
    "_normalize_mcp_input_schema",
    "mcp_prefixed_tool_name",
    "sanitize_mcp_name_component",
)


def test_original_namespace_reexports_are_identity_preserving():
    for name in _MOVED_NAMES:
        assert getattr(mcp_tool, name) is getattr(mcp_tool_schema, name)


def test_schema_module_imports_without_importing_god_module():
    code = (
        "import sys; import tools.mcp_tool_schema as schema; "
        "assert schema.__name__ == 'tools.mcp_tool_schema'; "
        "assert 'tools.mcp_tool' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_normalize_schema_preserves_provider_repairs():
    schema = {
        "definitions": {"Thing": {"type": "string"}},
        "type": "object",
        "properties": {
            "choice": {
                "anyOf": [
                    {"type": "string", "const": "one"},
                    {"type": "string", "const": "two"},
                    {"type": "null"},
                ],
                "default": None,
            }
        },
        "required": ["choice", "missing"],
    }
    result = mcp_tool._normalize_mcp_input_schema(schema)
    assert result["$defs"]["Thing"] == {"type": "string"}
    assert result["required"] == ["choice"]
    assert result["properties"]["choice"]["enum"] == ["one", "two"]
    assert result["properties"]["choice"]["nullable"] is True
    assert "definitions" not in result


def test_name_and_conversion_helpers_keep_legacy_behavior():
    assert mcp_tool.sanitize_mcp_name_component("my-server/value") == "my_server_value"
    assert mcp_tool.mcp_prefixed_tool_name("my-server", "list/tools") == "mcp__my_server__list_tools"
    listed = SimpleNamespace(
        name="list/tools",
        description="",
        inputSchema={"type": "object"},
    )
    converted = mcp_tool._convert_mcp_schema("my-server", listed)
    assert converted == {
        "name": "mcp__my_server__list_tools",
        "description": "MCP tool list/tools from my-server",
        "parameters": {"type": "object", "properties": {}},
    }


def test_utility_schema_builder_keeps_all_utility_shapes():
    schemas = mcp_tool._build_utility_schemas("srv")
    assert len(schemas) == 4
    assert [item["handler_key"] for item in schemas] == [
        "list_resources",
        "read_resource",
        "list_prompts",
        "get_prompt",
    ]
    assert [item["schema"]["name"] for item in schemas] == [
        "mcp__srv__list_resources",
        "mcp__srv__read_resource",
        "mcp__srv__list_prompts",
        "mcp__srv__get_prompt",
    ]
    assert schemas[1]["schema"]["parameters"]["required"] == ["uri"]
    assert schemas[3]["schema"]["parameters"]["required"] == ["name"]


def test_moved_callable_annotations_resolve_from_destination_module():
    for name in (
        "_normalize_mcp_input_schema",
        "mcp_prefixed_tool_name",
        "_convert_mcp_schema",
        "_build_utility_schemas",
    ):
        get_type_hints(getattr(mcp_tool, name))
        get_type_hints(getattr(mcp_tool_schema, name))
