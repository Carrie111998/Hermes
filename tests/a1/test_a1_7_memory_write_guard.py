"""Tests for A1.7 memory cross-session persistence boundary guard."""
import json
import pytest
from types import SimpleNamespace
from unittest.mock import patch
from agent.a1_7_memory_write_guard import check_memory_write_permission, MEMORY_SINKS
from agent.agent_runtime_helpers import invoke_tool


def _memory_dir() -> str:
    """Resolve the memory directory the way the guard does."""
    # Use get_memory_dir() directly so tests track the same canonical path
    from tools.memory_tool import get_memory_dir
    return str(get_memory_dir())


def _make_agent(classification=None, allowed_paths=None, source="hl_aos_frozen"):
    agent = SimpleNamespace()
    if classification is not None:
        agent.hl_aos_taint_classification = classification
    if allowed_paths is not None:
        agent.hl_aos_allowed_paths = allowed_paths
    agent.hl_aos_classification_source = source
    return agent


def _make_runtime_agent(classification=None, allowed_paths=None):
    agent = _make_agent(classification=classification, allowed_paths=allowed_paths)
    agent.session_id = "test-session"
    agent._current_turn_id = ""
    agent._current_api_request_id = ""
    agent._memory_manager = None
    agent._memory_store = object()
    return agent


class TestMemoryWriteGuard:
    """Test memory write classification enforcement.

    All tests resolve the real memory directory through get_memory_dir() so
    that the path-based check is tested on a real resolved path (not a
    fictional one that would trivially mismatch).
    """

    def test_c2_memory_write_denied_without_allowed_paths(self):
        """C2 agent without allowed_paths is denied memory writes."""
        agent = _make_agent(classification="C2")

        result = check_memory_write_permission(agent, "memory", "new entry")

        assert result is not None
        assert "denied" in result.lower()
        assert "hl_aos_allowed_paths" in result
        assert "C2" in result

    def test_c2_memory_write_with_allowed_paths(self):
        """C2 agent with memory dir in allowed_paths can write."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[memdir]
        )

        result = check_memory_write_permission(agent, "memory", "new entry")

        assert result is None  # allowed

    def test_c2_user_write_denied_when_only_memory_in_allowed(self):
        """C2 user write denied when only MEMORY.md is in allowed_paths."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[f"{memdir}/MEMORY.md"]
        )

        result = check_memory_write_permission(agent, "user", "user preference")

        assert result is not None
        assert "denied" in result.lower()
        assert "USER.md" in result

    def test_c0_memory_write_allowed(self):
        """C0 agent can write memory without restrictions."""
        agent = _make_agent(classification="C0")

        result = check_memory_write_permission(agent, "memory", "any content")

        assert result is None  # allowed

    def test_c1_memory_write_allowed(self):
        """C1 agent can write memory without restrictions."""
        agent = _make_agent(classification="C1")

        result = check_memory_write_permission(agent, "user", "user pref")

        assert result is None  # allowed

    def test_c3_memory_write_denied_without_allowed_paths(self):
        """C3 agent without allowed_paths is denied."""
        agent = _make_agent(classification="C3")

        result = check_memory_write_permission(agent, "memory", "classified note")

        assert result is not None
        assert "denied" in result.lower()
        assert "C3" in result

    def test_c4_memory_write_denied_without_allowed_paths(self):
        """C4 agent without allowed_paths is denied."""
        agent = _make_agent(classification="C4")

        result = check_memory_write_permission(agent, "memory", "top secret note")

        assert result is not None
        assert "denied" in result.lower()
        assert "C4" in result

    def test_missing_taint_classification_denied(self):
        """Agent with no taint classification is denied."""
        agent = SimpleNamespace()
        # No hl_aos_taint_classification attribute

        result = check_memory_write_permission(agent, "memory", "content")

        assert result is not None
        assert "no HL-AOS classification" in result

    def test_c2_memory_in_user_file_denied(self):
        """C2 agent denied writing to USER.md if only MEMORY.md is allowed."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[f"{memdir}/MEMORY.md"]
        )

        result = check_memory_write_permission(agent, "user", "user data")

        assert result is not None
        assert "denied" in result.lower()

    def test_c2_user_in_allowed_paths_allowed(self):
        """C2 agent with USER.md in allowed_paths can write to user store."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[f"{memdir}/USER.md"]
        )

        result = check_memory_write_permission(agent, "user", "user pref")

        assert result is None  # allowed

    def test_c2_both_stores_in_allowed_paths(self):
        """C2 agent with both files in allowed_paths can write to either."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[
                f"{memdir}/MEMORY.md",
                f"{memdir}/USER.md"
            ]
        )

        assert check_memory_write_permission(agent, "memory", "note") is None
        assert check_memory_write_permission(agent, "user", "pref") is None

    def test_c2_empty_allowed_paths_denied(self):
        """C2 agent with empty allowed_paths list is denied."""
        agent = _make_agent(
            classification="C2",
            allowed_paths=[]
        )

        result = check_memory_write_permission(agent, "memory", "content")

        assert result is not None
        assert "denied" in result.lower()

    def test_memory_sinks_constant(self):
        """MEMORY_SINKS contains only 'memory'."""
        assert "memory" in MEMORY_SINKS
        assert len(MEMORY_SINKS) == 1

    def test_c2_allowed_dir_subpath_match(self):
        """C2 allowed_paths matching parent dir permits both files."""
        memdir = _memory_dir()
        agent = _make_agent(
            classification="C2",
            allowed_paths=[memdir]
        )

        assert check_memory_write_permission(agent, "memory", "note") is None
        assert check_memory_write_permission(agent, "user", "pref") is None

    def test_invoke_tool_denies_before_memory_tool_execution(self):
        """Runtime memory branch denies C2 write before calling memory_tool."""
        agent = _make_runtime_agent(classification="C2")

        with patch("tools.memory_tool.memory_tool", side_effect=AssertionError("memory_tool should not run")) as mock_memory:
            result = invoke_tool(
                agent,
                "memory",
                {"target": "memory", "content": "synthetic C2 content"},
                "task-1",
                pre_tool_block_checked=True,
                skip_tool_request_middleware=True,
            )

        payload = json.loads(result)
        assert payload["success"] is False
        assert payload["denied_by"] == "a1_7_memory_guard"
        assert "hl_aos_allowed_paths" in payload["error"]
        mock_memory.assert_not_called()

    def test_invoke_tool_allows_memory_tool_when_memory_path_is_allowed(self):
        """Runtime memory branch calls memory_tool only after A1.7 allows it."""
        memdir = _memory_dir()
        agent = _make_runtime_agent(classification="C2", allowed_paths=[memdir])

        with patch("tools.memory_tool.memory_tool", return_value=json.dumps({"success": True})) as mock_memory:
            result = invoke_tool(
                agent,
                "memory",
                {"target": "memory", "content": "synthetic C2 content"},
                "task-1",
                pre_tool_block_checked=True,
                skip_tool_request_middleware=True,
            )

        assert json.loads(result) == {"success": True}
        mock_memory.assert_called_once()
