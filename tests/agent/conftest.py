"""Isolation guarantees shared by all agent unit tests."""

from __future__ import annotations

import ipaddress
import socket
import sys

import pytest


@pytest.fixture
def agent_network_attempts() -> list[object]:
    """Return public-network addresses blocked during the current test."""
    return []


def _is_loopback_socket_address(sock: socket.socket, address: object) -> bool:
    if sock.family not in {socket.AF_INET, socket.AF_INET6}:
        return True
    if not isinstance(address, tuple) or not address:
        return False

    host = str(address[0]).strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_agent_public_network(monkeypatch, agent_network_attempts):
    """Fail closed before agent unit tests can contact public services."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def guarded_connect(sock, address):
        if not _is_loopback_socket_address(sock, address):
            agent_network_attempts.append(address)
            raise RuntimeError(
                f"network disabled in agent unit tests: {address!r}"
            )
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_loopback_socket_address(sock, address):
            agent_network_attempts.append(address)
            raise RuntimeError(
                f"network disabled in agent unit tests: {address!r}"
            )
        return original_connect_ex(sock, address)

    def guarded_create_connection(address, *args, **kwargs):
        host = str(address[0]).strip().lower() if isinstance(address, tuple) and address else ""
        try:
            is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            agent_network_attempts.append(address)
            raise RuntimeError(
                f"network disabled in agent unit tests: {address!r}"
            )
        return original_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


@pytest.fixture(autouse=True)
def _reset_agent_auxiliary_state():
    """Prevent runtime bindings and cached clients from crossing test cases."""

    def reset() -> None:
        auxiliary_client = sys.modules.get("agent.auxiliary_client")
        if auxiliary_client is None:
            return
        auxiliary_client.shutdown_cached_clients()
        auxiliary_client.clear_runtime_main()
        auxiliary_client._reset_aux_unhealthy_cache()

    reset()
    yield
    reset()
