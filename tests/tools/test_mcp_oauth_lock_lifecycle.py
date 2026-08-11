"""Public-seam regressions for MCP OAuth lifecycle ownership.

These tests deliberately exercise the provider handed to HTTPX and the manager
entry point used by ``MCPServerTask._run_http``.  They do not replace the
SDK's response-driven control-plane protocol with a helper-only fake.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest


pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK OAuth support required")


async def _noop_redirect(_url: str) -> None:
    return None


async def _noop_callback() -> tuple[str, str | None]:
    raise AssertionError("callback must not run in generator lifecycle tests")


async def _provider(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    assert _HERMES_PROVIDER_CLS is not None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_manager_for_tests()
    storage = HermesTokenStorage("srv")
    # Seed enough durable state for the first data-plane request to be emitted
    # without opening a browser or performing registration.
    token = OAuthToken(
        access_token="access",
        token_type="Bearer",
        expires_in=3600,
        refresh_token="refresh",
    )
    client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    await storage.set_tokens(token)
    await storage.set_client_info(client)
    return _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://example.com/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            client_name="Hermes Agent",
        ),
        storage=storage,
        redirect_handler=_noop_redirect,
        callback_handler=_noop_callback,
    )


@pytest.mark.asyncio
async def test_outer_close_closes_inner_exactly_once(tmp_path, monkeypatch):
    """Closing the public auth generator releases its delegated inner flow."""
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    close_calls = 0

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise AssertionError("the flow is intentionally closed before response")

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda self, request: Inner())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()
    await flow.aclose()

    assert close_calls == 1


@pytest.mark.asyncio
async def test_inner_close_failure_does_not_mask_primary_exception(tmp_path, monkeypatch):
    """A provider/transport error remains observable if cleanup also fails."""
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    primary = RuntimeError("primary auth failure")
    cleanup = RuntimeError("secondary close failure")
    close_calls = 0

    @dataclass
    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise primary

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            raise cleanup

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda self, request: Inner())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()

    with pytest.raises(RuntimeError, match="primary auth failure") as raised:
        await flow.asend(httpx.Response(500))

    assert raised.value is primary
    assert close_calls == 1


@pytest.mark.asyncio
async def test_natural_completion_closes_inner_flow(tmp_path, monkeypatch):
    """Normal SDK completion also removes the delegated flow from ownership."""
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    close_calls = 0

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise StopAsyncIteration

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda self, request: Inner())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()

    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200))

    assert close_calls == 1


@pytest.mark.asyncio
async def test_incompatible_sdk_generator_fails_closed_before_transport(tmp_path, monkeypatch):
    """An SDK protocol drift cannot silently become an unauthenticated request."""
    from mcp.client.auth.oauth2 import OAuthClientProvider
    import tools.mcp_oauth_manager as manager_module

    expected_error = getattr(manager_module, "MCPAuthFlowProtocolError", RuntimeError)
    provider = await _provider(tmp_path, monkeypatch)

    class Incompatible:
        pass

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda self, request: Incompatible())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))

    with pytest.raises(expected_error):
        await flow.__anext__()


@pytest.mark.asyncio
async def test_cancellation_closes_inner_flow(tmp_path, monkeypatch):
    """Cancellation through the response-driven seam still runs delegated close."""
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    closed = asyncio.Event()

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise asyncio.CancelledError()

        async def aclose(self):
            closed.set()

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda self, request: Inner())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()

    with pytest.raises(asyncio.CancelledError):
        await flow.asend(httpx.Response(200))

    assert closed.is_set()


def test_changed_oauth_construction_config_rebuilds_provider(tmp_path, monkeypatch):
    """A cached endpoint must not reuse stale scope/redirect/timeout config."""
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    built = []

    def build(_server_name, entry):
        provider = type("Provider", (), {})()
        built.append((provider, entry.oauth_config))
        return provider

    monkeypatch.setattr(manager, "_build_provider", build)
    first = manager.get_or_build_provider(
        "srv",
        "https://example.com/mcp",
        {"scope": "read", "redirect_uri": "http://127.0.0.1:1/cb", "timeout": 30},
    )
    second = manager.get_or_build_provider(
        "srv",
        "https://example.com/mcp",
        {"scope": "write", "redirect_uri": "http://127.0.0.1:2/cb", "timeout": 5},
    )

    assert first is not second
    assert len(built) == 2
