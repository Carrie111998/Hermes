"""Integration tests for A1.6: tool write sinks respect HL-AOS classification.

These tests verify the write guard functions work correctly and can be called
from the tool registry's check_fn mechanism.

Run with:
    pytest tests/a1/test_a1_6_tool_write_sinks.py -v
"""

from types import SimpleNamespace
from pathlib import Path

import pytest

from tools.hl_aos_write_guard import check_write_permission, check_write_permission_with_context


def _make_agent_with_taint(classification=None, allowed_paths=None):
    """Create a minimal agent mock with taint attributes."""
    agent = SimpleNamespace(
        session_id="test-session",
        platform="test",
    )
    if classification is not None:
        agent.hl_aos_taint_classification = classification
    if allowed_paths is not None:
        agent.hl_aos_allowed_paths = allowed_paths
    return agent


class TestWritePermissionGuard:
    """Unit tests for hl_aos_write_guard.check_write_permission."""

    def test_denied_when_taint_missing(self, tmp_path):
        """Write denied when agent has no classification (fail-closed)."""
        agent = _make_agent_with_taint()
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert "denied" in str(result).lower()
        assert "A1.6" in str(result) or "classification" in str(result).lower()

    def test_denied_when_c2_no_allowed_paths(self, tmp_path):
        """Write denied when C2 session has no allowed_paths."""
        agent = _make_agent_with_taint(classification="C2")
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert "denied" in str(result).lower()
        assert "C2" in str(result)
        assert "allowed_paths" in str(result)

    def test_allowed_when_c2_with_matching_path(self, tmp_path):
        """Write allowed when C2 session has matching allowed_paths."""
        allowed_dir = str(tmp_path / "allowed")
        Path(allowed_dir).mkdir()
        agent = _make_agent_with_taint(
            classification="C2",
            allowed_paths=[allowed_dir]
        )
        target = str(tmp_path / "allowed" / "test.txt")

        result = check_write_permission(agent, target)

        assert result is None

    def test_allowed_when_c0(self, tmp_path):
        """Write allowed when C0 session (no restrictions)."""
        agent = _make_agent_with_taint(classification="C0")
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert result is None

    def test_allowed_when_c1(self, tmp_path):
        """Write allowed when C1 session (no restrictions)."""
        agent = _make_agent_with_taint(classification="C1")
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert result is None

    def test_denied_when_c3_no_allowed_paths(self, tmp_path):
        """Write denied when C3 session has no allowed_paths."""
        agent = _make_agent_with_taint(classification="C3")
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert "denied" in str(result).lower()
        assert "C3" in str(result)

    def test_denied_when_c4_no_allowed_paths(self, tmp_path):
        """Write denied when C4 session has no allowed_paths."""
        agent = _make_agent_with_taint(classification="C4")
        target = str(tmp_path / "test.txt")

        result = check_write_permission(agent, target)

        assert "denied" in str(result).lower()
        assert "C4" in str(result)

    def test_denied_when_c2_path_outside_allowed(self, tmp_path):
        """Write denied when C2 session target outside allowed_paths."""
        allowed_dir = str(tmp_path / "allowed")
        denied_dir = str(tmp_path / "denied")
        Path(allowed_dir).mkdir()
        Path(denied_dir).mkdir()
        
        agent = _make_agent_with_taint(
            classification="C2",
            allowed_paths=[allowed_dir]
        )
        target = str(tmp_path / "denied" / "test.txt")

        result = check_write_permission(agent, target)

        assert "denied" in str(result).lower()
        assert "C2" in str(result)

    def test_check_write_permission_with_context(self, tmp_path):
        """Context variant works the same as basic check."""
        agent = _make_agent_with_taint(classification="C0")
        target = str(tmp_path / "test.txt")

        result = check_write_permission_with_context(agent, target, {})

        assert result is None

    def test_multiple_allowed_paths(self, tmp_path):
        """Write allowed when target matches any allowed_path."""
        allowed1 = str(tmp_path / "allowed1")
        allowed2 = str(tmp_path / "allowed2")
        Path(allowed1).mkdir()
        Path(allowed2).mkdir()
        
        agent = _make_agent_with_taint(
            classification="C2",
            allowed_paths=[allowed1, allowed2]
        )
        
        target1 = str(tmp_path / "allowed1" / "test.txt")
        target2 = str(tmp_path / "allowed2" / "test.txt")

        assert check_write_permission(agent, target1) is None
        assert check_write_permission(agent, target2) is None
