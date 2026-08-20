"""Tests for Docker desktop session cwd normalization (Issue #90679).

When the desktop app creates a session with a Windows host path and the
terminal backend is Docker, that path must be translated to the container-side
mount point so terminal commands work.
"""

import pytest
from unittest.mock import patch, MagicMock
from tui_gateway.methods_session import _normalize_docker_session_cwd


class TestNormalizeDockerSessionCwd:
    """Test _normalize_docker_session_cwd function."""

    def test_windows_backslash_path_maps_to_workspace_for_docker(self):
        """Windows backslash paths (D:\\.hermes) map to /workspace for Docker."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("C:\\Users\\me", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("E:\\projects", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("F:\\data", "docker") == "/workspace"

    def test_windows_forward_slash_path_maps_to_workspace_for_docker(self):
        """Windows forward slash paths (D:/.hermes) map to /workspace for Docker."""
        assert _normalize_docker_session_cwd("D:/.hermes", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("C:/Users/me", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("E:/projects", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("F:/data", "docker") == "/workspace"

    def test_unix_paths_unchanged_for_docker(self):
        """Unix paths remain unchanged for Docker (they're already container paths)."""
        assert _normalize_docker_session_cwd("/home/user/workspace", "docker") == "/home/user/workspace"
        assert _normalize_docker_session_cwd("/workspace", "docker") == "/workspace"
        assert _normalize_docker_session_cwd("/root/project", "docker") == "/root/project"

    def test_relative_paths_unchanged_for_docker(self):
        """Relative paths remain unchanged for Docker."""
        assert _normalize_docker_session_cwd(".", "docker") == "."
        assert _normalize_docker_session_cwd("./src", "docker") == "./src"
        assert _normalize_docker_session_cwd("projects/hermes", "docker") == "projects/hermes"

    def test_windows_paths_unchanged_for_local_backend(self):
        """Windows paths remain unchanged for local backend."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "local") == "D:\\.hermes"
        assert _normalize_docker_session_cwd("C:/Users/me", "local") == "C:/Users/me"

    def test_windows_paths_unchanged_for_ssh_backend(self):
        """Windows paths remain unchanged for SSH backend (host-side resolution)."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "ssh") == "D:\\.hermes"
        assert _normalize_docker_session_cwd("C:/Users/me", "ssh") == "C:/Users/me"

    def test_windows_paths_unchanged_for_modal_backend(self):
        """Windows paths remain unchanged for Modal backend (different mount logic)."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "modal") == "D:\\.hermes"
        assert _normalize_docker_session_cwd("C:/Users/me", "modal") == "C:/Users/me"

    def test_empty_cwd_returns_empty(self):
        """Empty cwd returns empty string unchanged."""
        assert _normalize_docker_session_cwd("", "docker") == ""
        assert _normalize_docker_session_cwd("", "local") == ""

    def test_none_cwd_returns_none(self):
        """None cwd returns None unchanged."""
        assert _normalize_docker_session_cwd(None, "docker") is None
        assert _normalize_docker_session_cwd(None, "local") is None

    def test_empty_backend_returns_unchanged(self):
        """Empty backend returns cwd unchanged."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "") == "D:\\.hermes"
        assert _normalize_docker_session_cwd("D:\\.hermes", None) == "D:\\.hermes"

    def test_backend_case_insensitive(self):
        """Backend matching is case-insensitive."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "DOCKER") == "/workspace"
        assert _normalize_docker_session_cwd("D:\\.hermes", "Docker") == "/workspace"
        assert _normalize_docker_session_cwd("D:\\.hermes", "LOCAL") == "D:\\.hermes"

    def test_backend_whitespace_stripped(self):
        """Backend whitespace is stripped before comparison."""
        assert _normalize_docker_session_cwd("D:\\.hermes", "  docker  ") == "/workspace"
        assert _normalize_docker_session_cwd("D:\\.hermes", "\\tdocker\\n") == "/workspace"


class TestSessionCreateDockerIntegration:
    """Integration tests for session.create with Docker backend."""

    @pytest.fixture
    def mock_server_globals(self, monkeypatch):
        """Mock the server.py globals that methods_session relies on."""
        # Mock all the global functions that session.create calls
        mock_globals = {
            "uuid": MagicMock(),
            "_new_session_key": MagicMock(return_value="test_key"),
            "_coerce_seed_history": MagicMock(return_value=[]),
            "_completion_cwd": MagicMock(return_value="D:\\.hermes"),  # Windows host path
            "_resolve_session_source": MagicMock(return_value="desktop"),
            "_enable_gateway_prompts": MagicMock(),
            "_profile_home": MagicMock(return_value=None),
            "is_truthy_value": MagicMock(return_value=False),
            "_sessions": {},
            "_sessions_lock": MagicMock(__enter__=MagicMock(), __exit__=MagicMock()),
            "_register_session_cwd": MagicMock(),
            "_schedule_agent_build": MagicMock(),
            "_schedule_session_cap_enforcement": MagicMock(),
            "_ok": MagicMock(return_value={"result": "ok"}),
            "_history_to_messages": MagicMock(return_value=[]),
            "_resolve_model": MagicMock(return_value="claude-opus-4"),
            "_git_branch_for_cwd": MagicMock(return_value="main"),
            "_project_info_for_cwd": MagicMock(return_value=None),
            "_response_profile_name": MagicMock(return_value=None),
            "DESKTOP_BACKEND_CONTRACT": 1,
            "os": MagicMock(path=MagicMock(isdir=MagicMock(return_value=True))),
            "threading": MagicMock(Event=MagicMock(), Lock=MagicMock()),
            "time": MagicMock(time=MagicMock(return_value=1234567890.0)),
        }
        
        # Inject these into the methods_session module's namespace
        import tui_gateway.methods_session as ms
        for name, value in mock_globals.items():
            if not hasattr(ms, name):
                monkeypatch.setattr(ms, name, value, raising=False)
        
        return mock_globals

    def test_docker_backend_translates_windows_path_to_workspace(self, mock_server_globals, monkeypatch):
        """Desktop session.create with Docker backend translates D:\\.hermes to /workspace."""
        import tui_gateway.methods_session as ms
        
        # Mock _load_cfg to return Docker backend
        mock_load_cfg = MagicMock(return_value={"terminal": {"backend": "docker"}})
        monkeypatch.setattr(ms, "_load_cfg", mock_load_cfg)
        
        # Mock _completion_cwd to return Windows host path
        mock_server_globals["_completion_cwd"].return_value = "D:\\.hermes"
        
        # Create a session
        params = {"cwd": "D:\\.hermes", "source": "desktop"}
        
        # Get the handler function
        handler = None
        for item in ms._registry._methods:
            if item["name"] == "session.create":
                handler = item["handler"]
                break
        
        assert handler is not None, "session.create handler not found"
        
        # Call the handler
        result = handler(rid="test-rid", params=params)
        
        # Verify that the session's cwd was normalized to /workspace
        # The session dict is stored in _sessions[sid]
        assert len(mock_server_globals["_sessions"]) == 1
        session = list(mock_server_globals["_sessions"].values())[0]
        assert session["cwd"] == "/workspace", f"Expected /workspace but got {session['cwd']}"

    def test_local_backend_preserves_windows_path(self, mock_server_globals, monkeypatch):
        """Desktop session.create with local backend preserves D:\\.hermes unchanged."""
        import tui_gateway.methods_session as ms
        
        # Mock _load_cfg to return local backend
        mock_load_cfg = MagicMock(return_value={"terminal": {"backend": "local"}})
        monkeypatch.setattr(ms, "_load_cfg", mock_load_cfg)
        
        # Mock _completion_cwd to return Windows host path
        mock_server_globals["_completion_cwd"].return_value = "D:\\.hermes"
        
        # Create a session
        params = {"cwd": "D:\\.hermes", "source": "desktop"}
        
        # Get the handler function
        handler = None
        for item in ms._registry._methods:
            if item["name"] == "session.create":
                handler = item["handler"]
                break
        
        assert handler is not None, "session.create handler not found"
        
        # Call the handler
        result = handler(rid="test-rid", params=params)
        
        # Verify that the session's cwd was NOT normalized
        assert len(mock_server_globals["_sessions"]) == 1
        session = list(mock_server_globals["_sessions"].values())[0]
        assert session["cwd"] == "D:\\.hermes", f"Expected D:\\.hermes but got {session['cwd']}"

    def test_docker_backend_preserves_unix_path(self, mock_server_globals, monkeypatch):
        """Desktop session.create with Docker backend preserves /workspace unchanged."""
        import tui_gateway.methods_session as ms
        
        # Mock _load_cfg to return Docker backend
        mock_load_cfg = MagicMock(return_value={"terminal": {"backend": "docker"}})
        monkeypatch.setattr(ms, "_load_cfg", mock_load_cfg)
        
        # Mock _completion_cwd to return Unix container path
        mock_server_globals["_completion_cwd"].return_value = "/workspace"
        
        # Create a session
        params = {"cwd": "/workspace", "source": "desktop"}
        
        # Get the handler function
        handler = None
        for item in ms._registry._methods:
            if item["name"] == "session.create":
                handler = item["handler"]
                break
        
        assert handler is not None, "session.create handler not found"
        
        # Call the handler
        result = handler(rid="test-rid", params=params)
        
        # Verify that the session's cwd was preserved
        assert len(mock_server_globals["_sessions"]) == 1
        session = list(mock_server_globals["_sessions"].values())[0]
        assert session["cwd"] == "/workspace", f"Expected /workspace but got {session['cwd']}"
