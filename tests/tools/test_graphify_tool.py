import json
from pathlib import Path
import pytest
from tools.graphify_tool import _handle_graphify, GRAPHIFY_TOOL_SCHEMA
from tools.registry import registry


def test_graphify_tool_is_registered():
    all_tool_names = [entry.name for entry in registry.get_all_entries()]
    assert "graphify" in all_tool_names


def test_graphify_tool_schema():
    assert GRAPHIFY_TOOL_SCHEMA["name"] == "graphify"
    assert "action" in GRAPHIFY_TOOL_SCHEMA["parameters"]["required"]
    assert set(GRAPHIFY_TOOL_SCHEMA["parameters"]["properties"]["action"]["enum"]) == {
        "create", "update", "query", "understand"
    }


def test_graphify_handle_invalid_path(tmp_path):
    non_existent = tmp_path / "does_not_exist"
    res = _handle_graphify(action="understand", path=str(non_existent))
    data = json.loads(res)
    assert "error" in data
    assert "Target path does not exist" in data["error"]


def test_graphify_handle_understand_empty_dir(tmp_path):
    res = _handle_graphify(action="understand", path=str(tmp_path))
    data = json.loads(res)
    assert data.get("status") == "not_found"
    assert "No knowledge graph found" in data.get("message", "")
