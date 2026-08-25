"""MCP HTTP transports normalize environment proxy aliases before use."""

from __future__ import annotations

import asyncio

import pytest


def _set_bare_socks_proxy_env(monkeypatch, key: str) -> None:
    for env_key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv(key, "SOCKS://127.0.0.1:10808")
    monkeypatch.setenv("NO_PROXY", "example.com")


@pytest.mark.parametrize("proxy_key", ["ALL_PROXY", "all_proxy"])
def test_http_proxy_aliases_are_normalized_before_preflight(monkeypatch, proxy_key):
    from tools.mcp_tool import MCPServerTask, NonMcpEndpointError

    _set_bare_socks_proxy_env(monkeypatch, proxy_key)
    server = MCPServerTask("remote")

    async def _capture_preflight(self, *args, **kwargs):
        import os

        assert os.environ[proxy_key] == "socks5://127.0.0.1:10808"
        assert os.environ["NO_PROXY"] == "example.com"
        raise NonMcpEndpointError("stop after proxy assertion")

    monkeypatch.setattr(MCPServerTask, "_preflight_content_type", _capture_preflight)

    asyncio.run(server.run({"url": "https://example.com/mcp"}))


@pytest.mark.parametrize("proxy_key", ["ALL_PROXY", "all_proxy"])
def test_http_proxy_aliases_are_normalized_when_preflight_is_skipped(
    monkeypatch, proxy_key
):
    from tools.mcp_tool import MCPServerTask

    _set_bare_socks_proxy_env(monkeypatch, proxy_key)
    server = MCPServerTask("remote")
    seen: dict[str, str] = {}

    async def _capture_transport(self, config):
        import os

        seen["proxy"] = os.environ[proxy_key]
        server._shutdown_event.set()
        return "shutdown"

    monkeypatch.setattr(MCPServerTask, "_run_http", _capture_transport)

    asyncio.run(server.run({"url": "https://example.com/mcp", "skip_preflight": True}))

    assert seen == {"proxy": "socks5://127.0.0.1:10808"}
