"""Tests for OAuth server metadata persistence across process restarts.

Covers:
- :class:`HermesTokenStorage` ``.meta.json`` roundtrip (save / load / remove)
- The production manager provider
  (:class:`tools.mcp_oauth_manager.HermesMCPOAuthProvider`) restoring metadata
  on cold-load init and persisting metadata at the end of ``async_auth_flow``.

Context
=======
The MCP SDK discovers OAuth server metadata (``token_endpoint``, etc.)
on-demand and keeps it in memory only. Without disk persistence a restart
forces the SDK to fall back to guessing ``{server_url}/token``, which returns
404 on most real providers and triggers a full browser re-auth even when the
refresh token is still valid. These tests lock in the disk persistence
layer so refresh across restarts stays quiet.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp.shared.auth import OAuthMetadata

from tools.mcp_oauth import HermesTokenStorage
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
from tools.mcp_oauth_manager import _merge_oauth_control_request


@pytest.mark.asyncio
async def test_explicit_sdk_metadata_request_merges_safe_client_headers():
    """HTTPX ``send(Request)`` skips defaults; the OAuth seam must not."""
    import httpx

    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"X-Witness": "configured", "Authorization": "Bearer never"},
    ) as client:
        baseline_request = httpx.Request(
            "GET",
            "https://example.test/.well-known/oauth-authorization-server",
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
        baseline = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"X-Witness": "configured"},
        )
        try:
            await baseline.send(baseline_request)
        finally:
            await baseline.aclose()
        assert "x-witness" not in seen[0].headers

        request = httpx.Request(
            "POST",
            "https://example.test/.well-known/oauth-authorization-server",
            content=b"payload",
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
        merged = _merge_oauth_control_request(client, request)
        response = await client.send(merged)

        collision = httpx.Request(
            "GET",
            "https://example.test/.well-known/oauth-authorization-server",
            headers={"MCP-Protocol-Version": "2025-06-18", "x-witness": "request"},
        )
        await client.send(_merge_oauth_control_request(client, collision))

    assert response.status_code == 200
    assert seen[1].method == "POST"
    assert seen[1].content == b"payload"
    assert seen[1].headers["x-witness"] == "configured"
    assert seen[1].headers["mcp-protocol-version"] == "2025-06-18"
    assert "authorization" not in seen[1].headers
    assert seen[2].headers["x-witness"] == "request"
    assert "authorization" not in seen[2].headers


def _make_metadata(token_endpoint: str = "https://auth.example.com/oauth/token") -> OAuthMetadata:
    return OAuthMetadata.model_validate(
        {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/oauth/authorize",
            "token_endpoint": token_endpoint,
            "response_types_supported": ["code"],
        }
    )


# ---------------------------------------------------------------------------
# HermesTokenStorage metadata roundtrip
# ---------------------------------------------------------------------------


class TestMetadataStorage:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("example-server")

        meta = _make_metadata()
        storage.save_oauth_metadata(meta)

        meta_path = tmp_path / "mcp-tokens" / "example-server.meta.json"
        assert meta_path.exists()

        loaded = storage.load_oauth_metadata()
        assert loaded is not None
        assert str(loaded.token_endpoint) == "https://auth.example.com/oauth/token"
        assert str(loaded.issuer).rstrip("/") == "https://auth.example.com"


    def test_remove_deletes_meta_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("cleanup-server")

        storage.save_oauth_metadata(_make_metadata())
        assert storage._meta_path().exists()

        storage.remove()
        assert not storage._meta_path().exists()


# ---------------------------------------------------------------------------
# Manager-path provider (HermesMCPOAuthProvider) — production code path
# ---------------------------------------------------------------------------


def _manager_provider_with_context(storage: HermesTokenStorage, **context_attrs):
    """Build an uninitialized manager provider with a mocked context.

    Bypasses the full OAuthClientProvider init so we can exercise the
    override logic in isolation.
    """
    if _HERMES_PROVIDER_CLS is None:
        pytest.skip("MCP SDK auth not available")
    provider = _HERMES_PROVIDER_CLS.__new__(_HERMES_PROVIDER_CLS)
    provider._hermes_server_name = context_attrs.get("server_name", "srv")
    context = MagicMock()
    context.storage = storage
    context.oauth_metadata = context_attrs.get("oauth_metadata")
    context.current_tokens = context_attrs.get("current_tokens")
    context.server_url = context_attrs.get("server_url", "https://example.com")
    context.update_token_expiry = MagicMock()
    provider.context = context
    return provider


class TestManagerOAuthProviderMetadata:
    def test_initialize_restores_metadata_from_disk(self, tmp_path, monkeypatch):
        """Cold-load: if we have no in-memory metadata but disk has some, restore it."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("mgr-srv")
        storage.save_oauth_metadata(_make_metadata("https://mgr.example.com/token"))
        provider = _manager_provider_with_context(storage, oauth_metadata=None)

        with patch.object(
            _HERMES_PROVIDER_CLS.__bases__[0], "_initialize", new=AsyncMock()
        ):
            asyncio.run(provider._initialize())

        assert provider.context.oauth_metadata is not None
        assert str(provider.context.oauth_metadata.token_endpoint) == \
            "https://mgr.example.com/token"


    def test_async_auth_flow_persists_on_completion(self, tmp_path, monkeypatch):
        """End-to-end: running the wrapped auth_flow persists discovered metadata."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("flow-srv")
        provider = _manager_provider_with_context(
            storage,
            oauth_metadata=_make_metadata("https://flow.example.com/token"),
            server_name="flow-srv",
        )

        async def fake_parent_flow(self, request):
            if False:
                yield  # pragma: no cover -- make this an async generator
            return

        manager = MagicMock()
        manager.invalidate_if_disk_changed = AsyncMock(return_value=False)

        with patch.object(
            _HERMES_PROVIDER_CLS.__bases__[0],
            "async_auth_flow",
            new=fake_parent_flow,
        ), patch("tools.mcp_oauth_manager.get_manager", return_value=manager):
            async def drive():
                gen = provider.async_auth_flow(MagicMock())
                async for _ in gen:
                    pass

            asyncio.run(drive())

        loaded = storage.load_oauth_metadata()
        assert loaded is not None
        assert str(loaded.token_endpoint) == "https://flow.example.com/token"
