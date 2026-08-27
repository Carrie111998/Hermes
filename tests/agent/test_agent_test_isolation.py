"""Behavior contracts for agent-test network and auxiliary-state isolation."""

from __future__ import annotations

import socket

import pytest

import agent.auxiliary_client as auxiliary_client


def test_agent_tests_block_public_network(agent_network_attempts):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network disabled in agent unit tests"):
            sock.connect(("203.0.113.1", 443))
    finally:
        sock.close()

    assert agent_network_attempts == [("203.0.113.1", 443)]


def test_agent_tests_allow_loopback_network(agent_network_attempts):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        accepted.close()
    finally:
        client.close()
        listener.close()

    assert agent_network_attempts == []


def test_auxiliary_state_can_be_dirtied_for_cleanup_regression():
    auxiliary_client.set_runtime_main(
        provider="custom:test",
        model="test-model",
        base_url="https://provider.example/v1",
        api_key="test-key",
        api_mode="chat_completions",
    )
    assert auxiliary_client._normalize_main_runtime(None)["provider"] == "custom:test"


def test_auxiliary_state_is_clean_after_previous_test():
    assert auxiliary_client._normalize_main_runtime(None) == {}
    assert auxiliary_client._client_cache == {}


def test_each_test_has_a_positive_timeout(pytestconfig):
    assert float(pytestconfig.getini("timeout")) > 0
