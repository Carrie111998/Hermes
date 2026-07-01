"""Integration tests for A1.6 HL-AOS Write/Egress Guard in invoke_tool()"""
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from agent.agent_runtime_helpers import invoke_tool
from agent.hl_aos_write_guard import EGRESS_SINKS


def _parse(result):
    """Normalize invoke_tool result: always a parsed dict."""
    if isinstance(result, str):
        return json.loads(result)
    return result


class TestA1EgressGuardIntegration:
    """Test egress guard integration with invoke_tool()"""

    def _make_agent(self, classification, allowed_egress=None):
        """Helper to create a test agent with taint and allowlist"""
        agent = Mock(spec=[])
        agent.session_id = "test-session-001"
        agent._current_turn_id = "test-turn-001"
        agent._current_api_request_id = "test-request-001"
        
        if classification is not None:
            agent.hl_aos_taint_classification = classification
        if allowed_egress is not None:
            agent.hl_aos_allowed_egress = allowed_egress
        
        return agent

    def test_c2_terminal_blocked_without_allowlist(self):
        """C2 agent with empty allowlist is blocked from terminal"""
        agent = self._make_agent("C2", allowed_egress=[])
        func_args = {"command": "ls -la"}
        
        result = _parse(invoke_tool(agent, "terminal", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "C2" in result["error"]
        assert "terminal" in result["error"]

    def test_c2_web_fetch_blocked_without_allowlist(self):
        """C2 agent with empty allowlist is blocked from web_fetch"""
        agent = self._make_agent("C2", allowed_egress=[])
        func_args = {"url": "http://example.com"}
        
        result = _parse(invoke_tool(agent, "web_fetch", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "C2" in result["error"]

    def test_c2_web_search_blocked_without_allowlist(self):
        """C2 agent with empty allowlist is blocked from web_search"""
        agent = self._make_agent("C2", allowed_egress=[])
        func_args = {"query": "test"}
        
        result = _parse(invoke_tool(agent, "web_search", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "C2" in result["error"]

    def test_c3_terminal_blocked(self):
        """C3 agent with empty allowlist is blocked from terminal"""
        agent = self._make_agent("C3", allowed_egress=[])
        func_args = {"command": "whoami"}
        
        result = _parse(invoke_tool(agent, "terminal", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "C3" in result["error"]

    def test_c4_web_fetch_blocked(self):
        """C4 (Top Secret) with empty allowlist is blocked from web_fetch"""
        agent = self._make_agent("C4", allowed_egress=[])
        func_args = {"url": "http://evil.com"}
        
        result = _parse(invoke_tool(agent, "web_fetch", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "C4" in result["error"]

    def test_c2_terminal_allowed_when_in_allowlist(self):
        """C2 agent with terminal in allowlist can use terminal"""
        from agent.hl_aos_write_guard import check_egress_permission
        
        agent = self._make_agent("C2", allowed_egress=["terminal"])
        
        # Verify the guard itself returns None (allowed)
        result = check_egress_permission(agent, "terminal")
        assert result is None

    def test_c2_web_fetch_allowed_when_in_allowlist(self):
        """C2 agent with web_fetch in allowlist can use web_fetch"""
        from agent.hl_aos_write_guard import check_egress_permission
        
        agent = self._make_agent("C2", allowed_egress=["web_fetch"])
        
        result = check_egress_permission(agent, "web_fetch")
        assert result is None

    def test_non_egress_tool_not_blocked(self):
        """Non-egress tools are not blocked even for C2 without allowlist"""
        from agent.hl_aos_write_guard import check_egress_permission
        
        agent = self._make_agent("C2", allowed_egress=[])
        
        # read_file is not an egress tool
        result = check_egress_permission(agent, "read_file")
        assert result is None

    def test_missing_classification_blocks_egress(self):
        """Agent with no classification is blocked from egress tools (fail-closed)"""
        agent = self._make_agent(classification=None)
        func_args = {"command": "ls"}
        
        result = _parse(invoke_tool(agent, "terminal", func_args, effective_task_id="test"))
        
        assert result["status"] == "blocked"
        assert "classification" in result["error"].lower()

    def test_c0_unrestricted_egress(self):
        """C0 agent can access all egress tools without allowlist"""
        from agent.hl_aos_write_guard import check_egress_permission
        
        agent = self._make_agent("C0", allowed_egress=[])
        
        result = check_egress_permission(agent, "terminal")
        assert result is None

    def test_c1_unrestricted_egress(self):
        """C1 agent can access all egress tools without allowlist"""
        from agent.hl_aos_write_guard import check_egress_permission
        
        agent = self._make_agent("C1", allowed_egress=[])
        
        result = check_egress_permission(agent, "web_fetch")
        assert result is None

    def test_egress_guard_error_blocks_tool(self):
        """If guard errors, tool is blocked (fail-closed invariant)"""
        agent = self._make_agent("C2", allowed_egress=[])
        func_args = {"command": "ls"}
        
        # Mock check_egress_permission to raise an exception
        with patch('agent.hl_aos_write_guard.check_egress_permission', side_effect=RuntimeError("Guard crashed!")):
            result = _parse(invoke_tool(agent, "terminal", func_args, effective_task_id="test"))
        
        # Should still block (fail-closed)
        assert result["status"] == "blocked"
        assert "guard error" in result["error"].lower()
