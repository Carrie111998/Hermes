"""
Tests for A1.6 HL-AOS Write Sink Guard.

Verifies that write operations are properly gated based on classification.
"""

import pytest
from pathlib import Path
from agent.hl_aos_write_guard import check_write_permission


class MockAgent:
    """Mock agent for testing write guard."""
    def __init__(self, classification=None, allowed_paths=None):
        if classification:
            self.hl_aos_taint_classification = classification
        if allowed_paths is not None:
            self.hl_aos_allowed_paths = allowed_paths


def test_c0_no_restrictions():
    """C0 classification: writes allowed anywhere."""
    agent = MockAgent(classification="C0")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is None


def test_c1_no_restrictions():
    """C1 classification: writes allowed anywhere."""
    agent = MockAgent(classification="C1")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is None


def test_c2_with_allowed_paths():
    """C2 classification: writes allowed only in configured paths."""
    agent = MockAgent(
        classification="C2",
        allowed_paths=["/tmp/allowed", "/var/lib/data"]
    )
    # Should succeed
    assert check_write_permission(agent, "/tmp/allowed/test.py") is None
    assert check_write_permission(agent, "/var/lib/data/subdir/test.py") is None
    # Should fail
    assert check_write_permission(agent, "/tmp/denied/test.py") is not None
    assert "not within hl_aos_allowed_paths" in check_write_permission(agent, "/tmp/denied/test.py")


def test_c2_without_allowed_paths_denied():
    """C2 without allowed_paths: all writes denied."""
    agent = MockAgent(classification="C2")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "requires hl_aos_allowed_paths configuration" in result


def test_c2_with_empty_allowed_paths_denied():
    """C2 with empty allowed_paths: all writes denied."""
    agent = MockAgent(classification="C2", allowed_paths=[])
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "requires hl_aos_allowed_paths configuration" in result


def test_c3_requires_allowed_paths():
    """C3 classification: requires allowed_paths configuration."""
    agent = MockAgent(classification="C3")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "C3 session requires hl_aos_allowed_paths configuration" in result


def test_c4_requires_allowed_paths():
    """C4 classification: requires allowed_paths configuration."""
    agent = MockAgent(classification="C4")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "C4 session requires hl_aos_allowed_paths configuration" in result


def test_no_classification_denied():
    """No classification: fail-closed deny."""
    agent = MockAgent()
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "has no HL-AOS classification" in result


def test_unknown_classification_denied():
    """Unknown classification: fail-closed deny."""
    agent = MockAgent(classification="C5")
    result = check_write_permission(agent, "/tmp/test.py")
    assert result is not None
    assert "unknown classification" in result


def test_path_resolution_with_symlinks():
    """Path resolution should handle symlinks correctly."""
    # This test verifies that resolved paths are compared, not raw paths
    agent = MockAgent(
        classification="C2",
        allowed_paths=["/tmp"]
    )
    # /tmp/../tmp/test.py should resolve to /tmp/test.py
    result = check_write_permission(agent, "/tmp/../tmp/test.py")
    assert result is None


def test_path_with_trailing_slash():
    """Paths with trailing slashes should work."""
    agent = MockAgent(
        classification="C2",
        allowed_paths=["/tmp/allowed/"]
    )
    result = check_write_permission(agent, "/tmp/allowed/test.py")
    assert result is None
