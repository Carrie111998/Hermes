"""Tests for A1.6 HL-AOS Write Guard — egress control"""
import pytest
from agent.hl_aos_write_guard import check_egress_permission
from types import SimpleNamespace


class TestEgressPermission:
    """Test egress tool gating for C2/C3/C4 sessions"""

    def test_c0_session_can_use_all_egress_tools(self):
        """C0 classification allows all egress operations"""
        agent = SimpleNamespace(hl_aos_taint_classification="C0")
        
        result = check_egress_permission(agent, "terminal")
        assert result is None
        
        result = check_egress_permission(agent, "web_fetch")
        assert result is None
        
        result = check_egress_permission(agent, "web_search")
        assert result is None

    def test_c2_session_denied_egress_without_allowlist(self):
        """C2 classification denies egress without hl_aos_allowed_egress"""
        agent = SimpleNamespace(hl_aos_taint_classification="C2")
        
        result = check_egress_permission(agent, "terminal")
        assert result is not None
        assert "denied" in result.lower()
        assert "C2" in result

    def test_c2_session_allowed_egress_with_allowlist(self):
        """C2 classification allows egress when tool is in hl_aos_allowed_egress"""
        agent = SimpleNamespace(
            hl_aos_taint_classification="C2",
            hl_aos_allowed_egress=["web_fetch", "terminal"]
        )
        
        result = check_egress_permission(agent, "terminal")
        assert result is None
        
        result = check_egress_permission(agent, "web_fetch")
        assert result is None

    def test_c3_session_egress_gating(self):
        """C3 classification applies same egress gating as C2"""
        agent_no_allowlist = SimpleNamespace(hl_aos_taint_classification="C3")
        result = check_egress_permission(agent_no_allowlist, "terminal")
        assert result is not None
        assert "C3" in result
        
        agent_with_allowlist = SimpleNamespace(
            hl_aos_taint_classification="C3",
            hl_aos_allowed_egress=["browser"]
        )
        result = check_egress_permission(agent_with_allowlist, "browser")
        assert result is None

    def test_c4_session_egress_gating(self):
        """C4 classification applies same egress gating as C2/C3"""
        agent_no_allowlist = SimpleNamespace(hl_aos_taint_classification="C4")
        result = check_egress_permission(agent_no_allowlist, "web_search")
        assert result is not None
        assert "C4" in result
        
        agent_with_allowlist = SimpleNamespace(
            hl_aos_taint_classification="C4",
            hl_aos_allowed_egress=["web_search"]
        )
        result = check_egress_permission(agent_with_allowlist, "web_search")
        assert result is None

    def test_c2_session_egress_tool_not_in_allowlist(self):
        """C2 session denies egress tool not in allowlist"""
        agent = SimpleNamespace(
            hl_aos_taint_classification="C2",
            hl_aos_allowed_egress=["web_fetch"]
        )
        
        result = check_egress_permission(agent, "terminal")
        assert result is not None
        assert "not in" in result.lower() or "denied" in result.lower()

    def test_non_egress_tool_passes_through_for_c2(self):
        """Non-egress tools don't require allowlist for C2 sessions"""
        agent = SimpleNamespace(hl_aos_taint_classification="C2")
        
        # read_file, memory, patch are not egress sinks
        result = check_egress_permission(agent, "read_file")
        assert result is None
        
        result = check_egress_permission(agent, "memory")
        assert result is None

    def test_missing_classification_denies_egress(self):
        """Missing classification denies egress (fail-closed)"""
        agent = SimpleNamespace()  # No hl_aos_taint_classification
        
        result = check_egress_permission(agent, "terminal")
        assert result is not None
        assert "denied" in result.lower()

    def test_empty_allowlist_denies_all_egress(self):
        """Empty hl_aos_allowed_egress denies all egress tools"""
        agent = SimpleNamespace(
            hl_aos_taint_classification="C2",
            hl_aos_allowed_egress=[]
        )
        
        result = check_egress_permission(agent, "terminal")
        assert result is not None
        
        result = check_egress_permission(agent, "web_fetch")
        assert result is not None

    def test_egress_sink_list_includes_expected_tools(self):
        """EGRESS_SINKS contains the expected tool names"""
        from agent.hl_aos_write_guard import EGRESS_SINKS
        
        assert "terminal" in EGRESS_SINKS
        assert "web_fetch" in EGRESS_SINKS
        assert "web_search" in EGRESS_SINKS
        assert "browser" in EGRESS_SINKS
        assert "fetch" in EGRESS_SINKS
