"""
Test that dump_api_response_debug correctly writes response dump files.
This isolates testing of the helper directly and verifies the core functionality.
"""
import json
import os
import tempfile
import sys
from unittest.mock import MagicMock
import pytest

# Import the function we want to test
from agent.agent_runtime_helpers import dump_api_response_debug, _ra
from utils import atomic_json_write, env_var_enabled


def test_dump_api_response_debug_creates_correct_file(monkeypatch, tmp_path):
    """
    Verify that dump_api_response_debug creates a file with correct structure.
    Direct test without going through full conversation loop.
    """
    # 1. Setup a mocked agent with required attributes
    mock_agent = MagicMock()
    mock_agent.session_id = "test_session_123"
    mock_agent.logs_dir = tmp_path
    mock_agent.verbose_logging = True
    
    # Mock the needed methods/attributes on the agent
    mock_agent._safe_session_filename_component = lambda sid: sid.replace("/", "_").replace("\\", "_")[:50]
    mock_agent._vprint = lambda msg: None  # suppress printing
    mock_agent.logger = MagicMock()

    # Ensure atomic_json_write and env_var_enabled behave predictably
    from utils import atomic_json_write, env_var_enabled
    def mock_write(f, content, default=None):
        with open(f, 'w') as fp:
            fp.write(json.dumps(content, indent=2))
    monkeypatch.setattr('utils.atomic_json_write', mock_write)
    monkeypatch.setattr('utils.env_var_enabled', lambda var_name: var_name in os.environ)

    # 2. Test case 1: Dump a dictionary-style response
    print("Testing dictionary-style payload...")
    dump_response = {
        "content": None,
        "status": "error",
        "error_message": "Simulated API failure",
        "provider": "openai",
        "usage": {}
    }
    
    result_path = dump_api_response_debug(
        agent=mock_agent,
        response=dump_response,
        reason="terminal_rejection"
    )
    
    # Verify a file was created
    assert result_path is not None, "Expected a path return value"
    assert os.path.exists(result_path), "Response dump file was not created"
    
    # Verify file content is correct JSON
    with open(result_path, "r") as f:
        content = json.load(f)
    
    # Check that reason is preserved
    assert content["reason"] == "terminal_rejection"
    # Check status
    assert content["status"] == "error"
    # Check provider
    assert content["provider"] == "openai"
    
    print("✓ Dictionary-style dump works correctly")
    
    # 3. Test case 2: Dump an error payload with exception
    print("Testing error-type payload...")
    try:
        raise ValueError("Mock error")
    except Exception as exc:
        error_dump = dump_api_response_debug(
            agent=mock_agent,
            response=None,
            reason="network_error",
            error=exc
        )
    
    assert error_dump is not None, "Error dump should return a path"
    assert os.path.exists(error_dump), "Error dump file was not created"
    
    with open(error_dump, "r") as f:
        error_content = json.load(f)
    
    # Check error type is preserved
    assert error_content["error_type"] == "ValueError"
    # Check status path
    assert "error" in error_content or "status" in error_content
    
    print("✓ Error-type dump works correctly")


def test_response_dump_filename_is_safe_and_unique(tmp_path, monkeypatch):
    """
    Verify that generated dump filenames are filesystem-safe and have expected pattern.
    """
    mock_agent = MagicMock()
    mock_agent.session_id = "simple_session"
    mock_agent.logs_dir = tmp_path
    
    # Mock required methods/attributes
    mock_agent._safe_session_filename_component = lambda sid: sid.replace("/", "_")
    mock_agent._vprint = lambda x: None
    mock_agent.verbose_logging = False
    mock_agent.logger = MagicMock()
    
    dummy_file = dump_api_response_debug(
        agent=mock_agent,
        response={"status": "test"},
        reason="test_reason"
    )
    
    assert dummy_file is not None, "Must return a path"
    assert os.path.exists(dummy_file), "Dump file should be created"
    
    filename = os.path.basename(dummy_file)
    
    # Filename should follow pattern response_dump_*timestamp*.json
    assert filename.startswith("response_dump_")
    assert filename.endswith(".json")
    
    # Should not contain spaces or newlines (valid filesystem characters)
    assert " " not in filename
    assert "\n" not in filename
    
    print(f"✓ Filename safe: {filename}")


def test_env_var_controls_dumping(tmp_path, monkeypatch):
    """
    Verify that dump files are only created when HERMES_DUMP_REQUESTS is set.
    """
    mock_agent = MagicMock()
    mock_agent.session_id = "test_session"
    mock_agent.logs_dir = tmp_path
    mock_agent._safe_session_filename_component = lambda sid: "test_sid"
    mock_agent._vprint = lambda x: None
    mock_agent.verbose_logging = False
    mock_agent.logger = MagicMock()

    # Case 1: Variable NOT set -> no file created and None returned
    monkeypatch.delenv("HERMES_DUMP_REQUESTS", raising=False)
    dummy_file = dump_api_response_debug(
        agent=mock_agent,
        response={"test": "data"},
        reason="test"  # Include reason to make the dump interesting
    )
    assert dummy_file is None, "Dump should not be created when HERMES_DUMP_REQUESTS is not set"

    # Case 2: Variable IS set -> file should be created
    monkeypatch.setenv("HERMES_DUMP_REQUESTS", "1")
    dummy_file = dump_api_response_debug(
        agent=mock_agent,
        response={"test": "data"},
        reason="test"
    )
    assert dummy_file is not None, "Must return a path when HERMES_DUMP_REQUESTS is set"
    assert os.path.exists(dummy_file), "Dump file should be created when HERMES_DUMP_REQUESTS=1"
    files = list(tmp_path.glob("response_dump_*.json"))
    assert len(files) == 1, "Dump file should be created when HERMES_DUMP_REQUESTS=1"
    print("✓ Environment variable control works correctly")