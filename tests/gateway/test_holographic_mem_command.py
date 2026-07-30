import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key

@pytest.fixture
def mock_holographic_store():
    with patch("plugins.memory.holographic.HolographicMemoryStore") as mock:
        yield mock

@pytest.fixture
def mock_holographic_provider():
    with patch("plugins.memory.holographic.HolographicMemoryProvider") as mock:
        yield mock

def _make_source():
    from gateway.config import Platform
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )

def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )

def _make_runner():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:test:dm:1"
    return runner

@pytest.mark.asyncio
async def test_handle_mem_command_list_format_table(mock_holographic_provider):
    # Setup mock provider and handler
    mock_p_inst = mock_holographic_provider.return_value
    mock_p_inst._handle_fact_store.return_value = "TABLE_OUTPUT"
    
    runner = _make_runner()
    event = _make_event("/mem list --limit 5 --format table")
    
    # We need to make sure _handle_mem_command is accessible
    from gateway.slash_commands import GatewaySlashCommandsMixin
    # Manually bind the method to our runner instance for testing
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    with patch("plugins.memory.holographic._load_plugin_config", return_value={}):
        res = await runner._handle_mem_command(event)
    
    assert res == "TABLE_OUTPUT"
    mock_p_inst._handle_fact_store.assert_called_once()
    args = mock_p_inst._handle_fact_store.call_args[0][0]
    assert args["action"] == "list"
    assert args["output_format"] == "table"
    assert args["limit"] == 5

@pytest.mark.asyncio
async def test_handle_mem_command_tree_subprocess(mock_holographic_provider):
    runner = _make_runner()
    event = _make_event("/mem tree")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    mock_proc = MagicMock()
    mock_proc.stdout = "TREE_OUTPUT"
    mock_proc.stderr = ""
    
    mock_hermes_home = MagicMock()
    mock_hermes_home.__truediv__.return_value.__truediv__.return_value = "/fake/path/holographic_tree.py"
    
    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        with patch("gateway.run._hermes_home", mock_hermes_home):
            with patch("plugins.memory.holographic._load_plugin_config", return_value={}):
                res = await runner._handle_mem_command(event)
                
    assert res == "TREE_OUTPUT"
    mock_run.assert_called_once()
    cmd_args = mock_run.call_args[0][0]
    assert cmd_args[-1] == "/fake/path/holographic_tree.py"

@pytest.mark.asyncio
async def test_handle_mem_command_probe(mock_holographic_provider):
    mock_p_inst = mock_holographic_provider.return_value
    mock_p_inst._handle_fact_store.return_value = '{"facts": []}'
    
    runner = _make_runner()
    event = _make_event("/mem probe my_entity")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    with patch("plugins.memory.holographic._load_plugin_config", return_value={}):
        await runner._handle_mem_command(event)
        
    args = mock_p_inst._handle_fact_store.call_args[0][0]
    assert args["action"] == "probe"
    assert args["entity"] == "my_entity"

@pytest.mark.asyncio
async def test_handle_mem_command_search(mock_holographic_provider):
    mock_p_inst = mock_holographic_provider.return_value
    mock_p_inst._handle_fact_store.return_value = '{"facts": []}'
    
    runner = _make_runner()
    event = _make_event("/mem search some query terms")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    with patch("plugins.memory.holographic._load_plugin_config", return_value={}):
        await runner._handle_mem_command(event)
        
    args = mock_p_inst._handle_fact_store.call_args[0][0]
    assert args["action"] == "search"
    assert args["query"] == "some query terms"
