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
    with patch("plugins.memory.load_memory_provider") as mock_load, \
         patch("plugins.memory._get_active_memory_provider", return_value="holographic"):
        mock_provider = MagicMock()
        mock_provider.handle_tool_call.return_value = "OK"
        mock_load.return_value = mock_provider
        yield mock_provider

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
    mock_holographic_provider.handle_tool_call.return_value = "TABLE_OUTPUT"
    
    runner = _make_runner()
    event = _make_event("/holographic-memory list --limit 5 --format table")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    res = await runner._handle_mem_command(event)
    
    assert res == "TABLE_OUTPUT"
    mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
        "action": "list",
        "output_format": "table",
        "limit": 5,
    })

@pytest.mark.asyncio
async def test_handle_mem_command_tree_subprocess(mock_holographic_provider):
    runner = _make_runner()
    event = _make_event("/holographic-memory tree")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    mock_proc = MagicMock()
    mock_proc.stdout = "TREE_OUTPUT"
    mock_proc.stderr = ""
    
    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        res = await runner._handle_mem_command(event)
                
    assert res == "TREE_OUTPUT"
    mock_run.assert_called_once()
    cmd_args = mock_run.call_args[0][0]
    assert cmd_args[-1].endswith("holographic_tree.py")

@pytest.mark.asyncio
async def test_handle_mem_command_probe(mock_holographic_provider):
    mock_holographic_provider.handle_tool_call.return_value = '{"facts": []}'
    
    runner = _make_runner()
    event = _make_event("/holographic-memory probe my_entity")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    await runner._handle_mem_command(event)
        
    mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
        "action": "probe",
        "entity": "my_entity",
    })

@pytest.mark.asyncio
async def test_handle_mem_command_search(mock_holographic_provider):
    mock_holographic_provider.handle_tool_call.return_value = '{"facts": []}'
    
    runner = _make_runner()
    event = _make_event("/holographic-memory search some query terms")
    
    from gateway.slash_commands import GatewaySlashCommandsMixin
    runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)
    
    await runner._handle_mem_command(event)
        
    mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
        "action": "search",
        "query": "some query terms",
    })
