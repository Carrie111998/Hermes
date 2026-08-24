"""Test that kanban_create redacts secrets in body (#92354)."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure hermes_agent is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.kanban_tools import _handle_create
from agent.redact import redact_sensitive_text


def test_kanban_create_redacts_api_key_in_body(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with patch("hermes_cli.kanban_db.create_task") as mock_create:
        mock_create.return_value = {"id": "task-123", "body": "redacted"}

        body_with_secret = "Please use api_key=sk-test1234567890abcdef to call the API"
        _handle_create({"assignee": "test-profile", "title": "Test", "body": body_with_secret})

        # Verify create_task was called with redacted body
        args, kwargs = mock_create.call_args
        called_body = kwargs.get("body", args[2] if len(args) > 2 else None)
        
        # Body should be redacted (should not contain raw secret)
        assert "sk-test1234567890abcdef" not in str(called_body)
        assert called_body != body_with_secret


def test_kanban_create_normal_body_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with patch("hermes_cli.kanban_db.create_task") as mock_create:
        mock_create.return_value = {"id": "task-123"}

        normal_body = "Implement feature X as described in ticket #123"
        _handle_create({"assignee": "test-profile", "title": "Test", "body": normal_body})
        
        args, kwargs = mock_create.call_args
        called_body = kwargs.get("body", args[2] if len(args) > 2 else normal_body)
        assert "Implement feature X" in str(called_body)
