from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tools.mcp_tool as mcp_tool
from tools.mcp_protocol import ProtocolPolicy, StaleConnectionGenerationError


def _run(coro):
    return asyncio.run(coro)


def test_modern_liveness_uses_stateless_discover_only():
    async def drive():
        session = SimpleNamespace(
            send_discover=AsyncMock(return_value={"supportedVersions": ["2026-07-28"]}),
            send_ping=AsyncMock(),
            initialize=AsyncMock(),
            session_id=None,
        )
        server = mcp_tool.MCPServerTask("modern-reachability")
        server._connection_generation = 7
        server.negotiated_era = "modern"
        server.negotiated_protocol_version = "2026-07-28"
        server.session = session
        await server._keepalive_probe()
        session.send_discover.assert_awaited_once_with("2026-07-28")
        session.send_ping.assert_not_called()
        session.initialize.assert_not_called()
        assert session.session_id is None
        assert server.liveness_strategy == "stateless-discover"

    _run(drive())


def test_modern_liveness_rejects_stale_generation_result():
    async def drive():
        started = asyncio.Event()
        release = asyncio.Event()

        async def discover(_version):
            started.set()
            await release.wait()
            return {}

        session = SimpleNamespace(send_discover=discover, send_ping=AsyncMock())
        server = mcp_tool.MCPServerTask("stale-reachability")
        server._connection_generation = 1
        server.negotiated_era = "modern"
        server.negotiated_protocol_version = "2026-07-28"
        server.session = session
        probe = asyncio.create_task(server._keepalive_probe())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        server._connection_generation = 2
        server.session = SimpleNamespace()
        release.set()
        with pytest.raises(StaleConnectionGenerationError):
            await probe
        session.send_ping.assert_not_called()

    _run(drive())


def test_legacy_liveness_keeps_session_ping_semantics():
    async def drive():
        session = SimpleNamespace(send_ping=AsyncMock(), list_tools=AsyncMock())
        server = mcp_tool.MCPServerTask("legacy-session")
        server._connection_generation = 1
        server.negotiated_era = "legacy"
        server.session = session
        await server._keepalive_probe()
        session.send_ping.assert_awaited_once()
        session.list_tools.assert_not_called()
        assert server.liveness_strategy == "legacy-session-ping"

    _run(drive())


def test_status_exposes_protocol_lifecycle_without_secrets(monkeypatch):
    secret = "status-secret-token"
    config = {
        "remote": {
            "url": "https://mcp.example.test/rpc",
            "protocol": "2026-07-28",
            "headers": {"Authorization": f"Bearer {secret}"},
            "env": {"MCP_SECRET": secret},
        }
    }
    server = mcp_tool.MCPServerTask("remote")
    server._config = config["remote"]
    server.session = SimpleNamespace()
    server._protocol_policy = ProtocolPolicy.MODERN
    server._connection_generation = 4
    server.negotiated_era = "modern"
    server.negotiated_protocol_version = "2026-07-28"
    server.fallback_reason = None
    server.liveness_strategy = "stateless-discover"
    server.subscription_state = "active"
    server.catalogue_state = "current"
    server.cache_state = "fresh"

    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: config)
    with mcp_tool._lock:
        saved_servers = dict(mcp_tool._servers)
        mcp_tool._servers.clear()
        mcp_tool._servers["remote"] = server
    try:
        status = mcp_tool.get_mcp_status()
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers.update(saved_servers)

    protocol = status[0]["protocol"]
    assert protocol["configured_policy"] == "modern"
    assert protocol["negotiated_era"] == "modern"
    assert protocol["negotiated_protocol_version"] == "2026-07-28"
    assert protocol["connection_generation"] == 4
    assert protocol["fallback_reason"] is None
    assert protocol["legacy_proof_attempted"] is False
    assert protocol["liveness_strategy"] == "stateless-discover"
    assert protocol["subscription_state"] == "active"
    assert protocol["catalogue_state"] == "current"
    assert protocol["cache_state"] == "fresh"
    assert protocol["mcp_sdk_version"]
    encoded = json.dumps(status)
    assert secret not in encoded
    assert "Authorization" not in encoded
    assert "MCP_SECRET" not in encoded


def test_status_exposes_policy_before_connection_without_secrets(monkeypatch):
    secret = "configured-status-secret"
    config = {
        "pending": {
            "command": "pending-server",
            "protocol": "stateless",
            "env": {"MCP_SECRET": secret},
        }
    }
    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: config)
    with mcp_tool._lock:
        saved_servers = dict(mcp_tool._servers)
        mcp_tool._servers.clear()
    try:
        status = mcp_tool.get_mcp_status()
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers.update(saved_servers)

    assert status[0]["status"] == "configured"
    assert status[0]["protocol"]["configured_policy"] == "modern"
    assert status[0]["protocol"]["negotiated_era"] is None
    assert secret not in json.dumps(status)
