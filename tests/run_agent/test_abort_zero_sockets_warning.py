"""Tests verifying that client aborts log warnings when 0 sockets are force-closed."""
import logging
from unittest.mock import patch, MagicMock


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://custom.example.com/v1",
        provider="custom",
        model="deepseek-v4-pro",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    return agent


def test_abort_request_openai_client_logs_warning_when_zero_sockets(caplog):
    agent = _make_agent()
    mock_client = MagicMock()
    with patch.object(agent, "_force_close_tcp_sockets", return_value=0):
        with caplog.at_level(logging.WARNING):
            agent._abort_request_openai_client(mock_client, reason="test_reason")
    assert "OpenAI client abort found 0 active sockets" in caplog.text


def test_abort_request_anthropic_client_logs_warning_when_zero_sockets(caplog):
    agent = _make_agent()
    mock_client = MagicMock()
    with patch.object(agent, "_force_close_tcp_sockets", return_value=0):
        with caplog.at_level(logging.WARNING):
            agent._abort_request_anthropic_client(mock_client, reason="test_reason")
    assert "Anthropic client abort found 0 active sockets" in caplog.text


def test_abort_request_openai_client_logs_info_when_sockets_closed(caplog):
    agent = _make_agent()
    mock_client = MagicMock()
    with patch.object(agent, "_force_close_tcp_sockets", return_value=1):
        with caplog.at_level(logging.INFO):
            agent._abort_request_openai_client(mock_client, reason="test_reason")
    assert "OpenAI client aborted" in caplog.text
    assert "found 0 active sockets" not in caplog.text
