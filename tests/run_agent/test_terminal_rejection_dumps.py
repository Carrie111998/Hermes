import os
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

# Setup minimal environment for run_agent
sys.modules.setdefault("fire", SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", SimpleNamespace())

import run_agent

@pytest.fixture
def temp_logs(tmp_path):
    return tmp_path

@pytest.fixture
def mock_agent(monkeypatch, temp_logs):
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    
    # Mock tool definitions
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    
    agent = run_agent.AIAgent(
        model="test-model",
        base_url="https://test.api",
        api_key="test-key",
        quiet_mode=True,
        max_iterations=1
    )
    agent.logs_dir = temp_logs
    return agent

def test_terminal_rejection_creates_response_dump(monkeypatch, mock_agent, temp_logs):
    # 1. Setup environment
    monkeypatch.setattr(os, "environ", {**os.environ, "HERMES_DUMP_REQUESTS": "1"})
    
    # 2. Setup failure scenario: API returns None (invalid response)
    # We need to drive it through max_retries.
    # Let's set max_retries = 1 for speed.
    monkeypatch.setattr(mock_agent, "max_retries", 1)
    
    call_count = 0
    def _fake_api_call(api_kwargs):
        nonlocal call_count
        call_count += 1
        return None # Trigger response_invalid = True

    monkeypatch.setattr(mock_agent, "_interruptible_api_call", _fake_api_call)
    
    # 3. Run conversation
    result = mock_agent.run_conversation("hello")
    
    # 4. Verify
    assert result["completed"] is False
    assert result["failed"] is True
    
    # Find the dump file
    dump_files = list(temp_logs.glob("response_dump_*.json"))
    assert len(dump_files) == 1, f"Expected 1 response dump file, found {len(dump_files)}"
    
    dump_file = dump_files[0]
    with open(dump_file, 'r') as f:
        data = json.load(f)
    
    assert data["reason"] == "terminal_rejection"
    # Note: In conversation_loop.py, _failure_hint is used.
    # If it's "response is None", we look for that.
    assert "response is None" in str(data["response"])
    assert data["response"]["status"] == "error"
