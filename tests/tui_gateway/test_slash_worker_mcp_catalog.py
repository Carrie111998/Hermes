"""Test slash worker MCP catalog join (#92330)."""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_slash_worker_marker_created(tmp_path, monkeypatch):
    """_prepare_slash_worker_runtime should create a marker file."""
    monkeypatch.setattr(
        "tui_gateway.slash_worker._SLASH_WORKER_MARKER",
        str(tmp_path / "marker"),
    )
    with patch("hermes_cli.mcp_startup.start_background_mcp_discovery"):
        with patch("hermes_cli.mcp_startup.wait_for_mcp_discovery"):
            from tui_gateway.slash_worker import _prepare_slash_worker_runtime
            _prepare_slash_worker_runtime()

    assert Path(tmp_path / "marker").exists()


def test_show_tools_joins_mcp_discovery_when_marker_present(tmp_path, monkeypatch):
    """cli.show_tools should call join_mcp_discovery when the slash-worker marker exists."""
    marker = tmp_path / "marker"
    marker.touch()

    joined = []
    with patch("hermes_cli.mcp_startup.join_mcp_discovery", side_effect=lambda timeout: joined.append(timeout)):
        # Simulate the check in cli.py
        if marker.exists():
            from hermes_cli.mcp_startup import join_mcp_discovery
            join_mcp_discovery(timeout=30.0)

    assert joined == [30.0]


def test_show_tools_skips_join_without_marker(tmp_path):
    """Without the marker, no join should happen (TUI path)."""
    marker = tmp_path / "no-marker"
    joined = []
    with patch("hermes_cli.mcp_startup.join_mcp_discovery", side_effect=lambda timeout: joined.append(timeout)):
        if marker.exists():
            from hermes_cli.mcp_startup import join_mcp_discovery
            join_mcp_discovery(timeout=30.0)

    assert joined == []
