"""Public-entry regressions for the MCP OAuth review remediation."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest


pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK OAuth support required")


async def _noop_redirect(_url: str) -> None:
    return None


async def _noop_callback() -> tuple[str, str | None]:
    raise AssertionError("callback must not run")


async def _provider(tmp_path, monkeypatch):
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("srv")
    await storage.set_tokens(
        OAuthToken(
            access_token="access",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="refresh",
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )
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
async def test_outer_aclose_surfaces_inner_cleanup_failure(tmp_path, monkeypatch):
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    cleanup_error = RuntimeError("close failed")

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise AssertionError("the flow is closed before a response")

        async def aclose(self):
            raise cleanup_error

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda *_args: Inner())
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()

    with pytest.raises(RuntimeError, match="close failed"):
        await flow.aclose()


@pytest.mark.asyncio
async def test_public_outer_close_releases_real_sdk_context_lock(tmp_path, monkeypatch):
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    inner_closed = asyncio.Event()

    async def real_sdk_flow(self, request):
        async with self.context.lock:
            try:
                yield request
            finally:
                inner_closed.set()

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", real_sdk_flow)
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    assert await flow.__anext__()
    await flow.aclose()
    assert inner_closed.is_set()
    # The public lock remains reacquirable without relying on GC or private
    # AnyIO lock internals as the cleanup oracle.
    async with provider.context.lock:
        pass


@pytest.mark.asyncio
async def test_loop_mismatch_rejects_before_inner_construction(tmp_path, monkeypatch):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from tools.mcp_oauth_manager import MCPAuthFlowLifecycleError

    provider = await _provider(tmp_path, monkeypatch)
    owner = asyncio.new_event_loop()
    owner.close()
    provider._hermes_loop = owner
    constructed = False

    def base_flow(*_args):
        nonlocal constructed
        constructed = True
        raise AssertionError("SDK inner flow must not be constructed")

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", base_flow)
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))

    with pytest.raises(MCPAuthFlowLifecycleError):
        await flow.__anext__()
    assert constructed is False


@pytest.mark.asyncio
async def test_live_cross_loop_provider_is_rejected(tmp_path, monkeypatch):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from tools.mcp_oauth_manager import MCPAuthFlowLifecycleError

    provider = await _provider(tmp_path, monkeypatch)
    owner = asyncio.new_event_loop()
    started = threading.Event()
    thread = threading.Thread(
        target=lambda: (started.set(), owner.run_forever()), daemon=True
    )
    thread.start()
    assert started.wait(2)
    provider._hermes_loop = owner

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise AssertionError

        async def aclose(self):
            return None

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda *_args: Inner())
    try:
        flow = provider.async_auth_flow(
            httpx.Request("POST", "https://example.com/mcp")
        )
        with pytest.raises(MCPAuthFlowLifecycleError):
            await flow.__anext__()
    finally:
        owner.call_soon_threadsafe(owner.stop)
        thread.join(timeout=2)
        owner.close()


def test_non_json_oauth_config_fails_closed(tmp_path, monkeypatch):
    from tools.mcp_oauth_manager import MCPAuthConfigurationError, MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(MCPAuthConfigurationError):
        MCPOAuthManager().get_or_build_provider(
            "srv", "https://example.com/mcp", {"scope": object()}
        )


@pytest.mark.asyncio
async def test_active_owner_flow_is_closed_before_replacement(tmp_path, monkeypatch):
    from tools.mcp_oauth_manager import (
        MCPAuthFlowLifecycleError,
        MCPOAuthManager,
        _ProviderEntry,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class OldFlow:
        async def aclose(self):
            close_started.set()
            await release_close.wait()

    old_flow = OldFlow()
    old_provider = SimpleNamespace(
        _hermes_active_flows={id(old_flow): old_flow},
        _hermes_loop=asyncio.get_running_loop(),
    )
    entry = _ProviderEntry(
        server_url="https://old.example/mcp",
        oauth_config={},
        provider=old_provider,
        loop=asyncio.get_running_loop(),
        oauth_config_fingerprint="old",
    )
    manager._entries[manager._key("srv")] = entry
    replacement = SimpleNamespace()
    monkeypatch.setattr(manager, "_build_provider", lambda *_args: replacement)

    with pytest.raises(MCPAuthFlowLifecycleError):
        manager.get_or_build_provider("srv", "https://new.example/mcp", {})
    assert manager._entries[manager._key("srv")] is entry
    assert old_provider._hermes_active_flows == {id(old_flow): old_flow}
    assert not close_started.is_set()
    release_close.set()


@pytest.mark.asyncio
async def test_explicit_retry_coalesces_with_scheduled_owner_cleanup(
    tmp_path, monkeypatch
):
    """A retry cannot enter a flow while automatic owner cleanup is running."""
    from tools.mcp_oauth_manager import MCPOAuthManager, _ProviderEntry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    class Flow:
        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            if close_calls > 1:
                raise AssertionError("duplicate concurrent owner cleanup")
            close_started.set()
            await release_close.wait()

    flow = Flow()
    provider = SimpleNamespace(
        _hermes_active_flows={id(flow): flow},
        _hermes_loop=asyncio.get_running_loop(),
        _hermes_generation=0,
    )
    entry = _ProviderEntry(
        server_url="https://old.example/mcp",
        oauth_config={},
        provider=provider,
        loop=asyncio.get_running_loop(),
        oauth_config_fingerprint="old",
    )
    manager._entries[manager._key("srv")] = entry

    assert manager._fence_and_schedule_close(entry) is False
    await close_started.wait()
    retry = asyncio.create_task(manager.retry_active_flow_cleanup("srv"))
    # Yield only to let the already-created public retry task reach its
    # owner-task await; the held event barrier, not elapsed time, proves the
    # first close remains the sole owner.
    await asyncio.sleep(0)
    assert not retry.done()
    assert close_calls == 1

    release_close.set()
    assert await retry is True
    await asyncio.sleep(0)
    assert close_calls == 1
    assert provider._hermes_active_flows == {}


@pytest.mark.asyncio
async def test_failed_owner_cleanup_is_retained_until_explicit_retry(
    tmp_path, monkeypatch
):
    """A failed close poisons the entry and cannot coexist with a replacement."""
    from tools.mcp_oauth_manager import (
        MCPAuthFlowLifecycleError,
        MCPOAuthManager,
        _ProviderEntry,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    close_done = asyncio.Event()
    attempts = 0

    class FlakyFlow:
        async def aclose(self):
            nonlocal attempts
            attempts += 1
            close_done.set()
            if attempts <= 2:
                raise RuntimeError("cleanup failed")

    old_flow = FlakyFlow()
    old_provider = SimpleNamespace(
        _hermes_active_flows={id(old_flow): old_flow},
        _hermes_loop=asyncio.get_running_loop(),
        _hermes_generation=0,
    )
    entry = _ProviderEntry(
        server_url="https://old.example/mcp",
        oauth_config={},
        provider=old_provider,
        loop=asyncio.get_running_loop(),
        oauth_config_fingerprint="old",
    )
    manager._entries[manager._key("srv")] = entry
    replacement = SimpleNamespace()
    built = 0

    def build(_server_name, _entry):
        nonlocal built
        built += 1
        return replacement

    monkeypatch.setattr(manager, "_build_provider", build)
    with pytest.raises(MCPAuthFlowLifecycleError):
        manager.get_or_build_provider("srv", "https://new.example/mcp", {})
    await close_done.wait()

    assert attempts == 1
    assert old_provider._hermes_active_flows == {id(old_flow): old_flow}
    assert built == 0

    assert await manager.retry_active_flow_cleanup("srv") is False
    assert attempts == 2
    assert old_provider._hermes_active_flows == {id(old_flow): old_flow}
    assert await manager.retry_active_flow_cleanup("srv") is True
    assert attempts == 3
    assert old_provider._hermes_active_flows == {}

    assert (
        manager.get_or_build_provider("srv", "https://new.example/mcp", {})
        is replacement
    )
    assert built == 1


def test_resolved_callback_identity_is_retained_after_build(tmp_path, monkeypatch):
    """A resolved callback port participates in the post-build cache identity."""
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    builds = 0

    def build(_server_name, _entry):
        nonlocal builds
        builds += 1
        return SimpleNamespace(_hermes_resolved_port=43127)

    monkeypatch.setattr(manager, "_build_provider", build)
    provider = manager.get_or_build_provider(
        "resolved", "https://example.test/mcp", {"redirect_host": "127.0.0.1"}
    )
    entry = manager._entries[manager._key("resolved")]
    assert provider is not None
    assert entry.resolved_callback_fingerprint == "127.0.0.1:43127"
    assert entry.oauth_config_fingerprint != entry.requested_fingerprint
    assert (
        manager.get_or_build_provider(
            "resolved", "https://example.test/mcp", {"redirect_host": "127.0.0.1"}
        )
        is provider
    )
    assert builds == 1


def test_effective_transport_policy_rebuilds_and_identical_policy_reuses(
    tmp_path, monkeypatch
):
    """Every load-bearing transport policy input participates in public cache identity."""
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    builds = []

    monkeypatch.setattr(
        manager,
        "_build_provider",
        lambda _name, _entry: builds.append(SimpleNamespace()) or builds[-1],
    )
    base = {
        "ssl_verify": "ca.pem",
        "client_cert": ("client.pem", "client.key"),
        "connect_timeout": 2,
        "read_timeout": 9,
        "follow_redirects": True,
        "headers": {"x-policy": "one"},
        "request_hooks": ["request-hook"],
        "response_hooks": ["response-hook"],
    }
    first = manager.get_or_build_provider(
        "policy", "https://example.test/mcp", {}, base
    )
    same = manager.get_or_build_provider(
        "policy", "https://example.test/mcp", {}, dict(base)
    )
    assert same is first
    assert len(builds) == 1

    for field, value in (
        ("ssl_verify", "other-ca.pem"),
        ("client_cert", ("other-client.pem", "other-client.key")),
        ("connect_timeout", 3),
        ("read_timeout", 10),
        ("follow_redirects", False),
        ("headers", {"x-policy": "two"}),
        ("request_hooks", ["other-request-hook"]),
        ("response_hooks", ["other-response-hook"]),
    ):
        changed = dict(base)
        changed[field] = value
        rebuilt = manager.get_or_build_provider(
            "policy", "https://example.test/mcp", {}, changed
        )
        assert rebuilt is not first
        first = rebuilt
        base = changed
    assert len(builds) == 9


@pytest.mark.asyncio
async def test_cross_owner_cleanup_failure_is_fenced_until_owner_retry(
    tmp_path, monkeypatch
):
    """A foreign owner failure is retained and later retried on that owner."""
    from tools.mcp_oauth_manager import MCPOAuthManager, MCPAuthFlowLifecycleError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MCPOAuthManager()
    old_provider = SimpleNamespace(_hermes_active_flows={})
    monkeypatch.setattr(manager, "_build_provider", lambda *_args: old_provider)
    manager.get_or_build_provider("cross", "https://old.example/mcp", {})
    entry = manager._entries[manager._key("cross")]
    owner = asyncio.new_event_loop()
    owner_thread = threading.Thread(target=owner.run_forever)
    owner_thread.start()
    attempts = 0

    class Flow:
        async def aclose(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("foreign close failed")

    flow = Flow()
    old_provider._hermes_active_flows[id(flow)] = flow
    entry.loop = owner
    old_provider._hermes_loop = owner
    try:
        with pytest.raises(MCPAuthFlowLifecycleError):
            manager.get_or_build_provider("cross", "https://new.example/mcp", {})
        assert old_provider._hermes_active_flows == {id(flow): flow}
        assert await manager.retry_active_flow_cleanup("cross") is True
        assert attempts == 2
        assert old_provider._hermes_active_flows == {}
    finally:
        owner.call_soon_threadsafe(owner.stop)
        owner_thread.join(timeout=5)
        owner.close()


@pytest.mark.asyncio
async def test_incompatible_inner_with_aclose_is_closed_before_protocol_error(
    tmp_path, monkeypatch
):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from tools.mcp_oauth_manager import MCPAuthFlowProtocolError

    provider = await _provider(tmp_path, monkeypatch)
    closed = 0

    class Incompatible:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def aclose(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        OAuthClientProvider, "async_auth_flow", lambda *_: Incompatible()
    )
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    with pytest.raises(MCPAuthFlowProtocolError):
        await flow.__anext__()
    assert closed == 1
    assert provider._hermes_active_flows == {}


@pytest.mark.asyncio
async def test_terminal_metadata_persistence_error_is_primary_over_close_error(
    tmp_path, monkeypatch
):
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    persistence = ValueError("persistence-primary")
    cleanup = RuntimeError("cleanup-secondary")

    class Inner:
        async def __anext__(self):
            return httpx.Request("POST", "https://example.com/mcp")

        async def asend(self, _response):
            raise StopAsyncIteration

        async def aclose(self):
            raise cleanup

    monkeypatch.setattr(OAuthClientProvider, "async_auth_flow", lambda *_: Inner())
    monkeypatch.setattr(
        provider,
        "_persist_oauth_metadata_if_changed",
        lambda: (_ for _ in ()).throw(persistence),
    )
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    await flow.__anext__()
    with pytest.raises(ValueError, match="persistence-primary") as raised:
        await flow.asend(httpx.Response(200))
    assert raised.value is persistence


@pytest.mark.asyncio
async def test_refresh_failure_preserves_primary_when_invalidation_fails(
    tmp_path, monkeypatch
):
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    primary = RuntimeError("refresh-primary")
    secondary = OSError("unlink-secondary")

    async def failing_refresh(*_args):
        raise primary

    monkeypatch.setattr(
        OAuthClientProvider, "_handle_refresh_response", failing_refresh
    )
    monkeypatch.setattr(
        provider.context.storage,
        "invalidate_tokens",
        lambda: (_ for _ in ()).throw(secondary),
    )
    with pytest.raises(RuntimeError, match="refresh-primary") as raised:
        await provider._handle_refresh_response(httpx.Response(500))
    assert raised.value is primary
    assert provider.context.current_tokens is None


@pytest.mark.asyncio
async def test_failed_refresh_invalidates_durable_bearer(tmp_path, monkeypatch):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthToken

    provider = await _provider(tmp_path, monkeypatch)
    await provider.context.storage.set_tokens(
        OAuthToken(
            access_token="stale",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="refresh",
        )
    )
    monkeypatch.setattr(
        OAuthClientProvider,
        "_handle_refresh_response",
        lambda *_args: _false_async(),
    )

    assert await provider._handle_refresh_response(httpx.Response(500)) is False
    assert await provider.context.storage.get_tokens() is None


async def _false_async():
    return False


@pytest.mark.asyncio
async def test_cold_prm_uses_sdk_resource_validation(tmp_path, monkeypatch):
    import httpx as httpx_module
    from mcp.shared.auth import OAuthClientMetadata, OAuthToken
    from pydantic import AnyUrl

    provider = await _provider(tmp_path, monkeypatch)
    await provider.context.storage.set_tokens(
        OAuthToken(
            access_token="access",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="refresh",
        )
    )
    provider.context.oauth_metadata = None
    calls = []

    async def validate(prm):
        calls.append(prm)
        raise RuntimeError("resource mismatch")

    monkeypatch.setattr(provider, "_validate_resource_match", validate)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth-protected-resource" in str(request.url):
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://other.example",
                    "authorization_servers": ["https://auth.example"],
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx_module.MockTransport(handler)
    original = httpx_module.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx_module, "AsyncClient", client)
    with pytest.raises(RuntimeError, match="resource mismatch"):
        await provider._prefetch_oauth_metadata()
    assert calls


@pytest.mark.asyncio
async def test_cold_control_requests_use_tls_policy_without_bearer(
    tmp_path, monkeypatch
):
    import mcp.client.auth.utils as auth_utils

    provider = await _provider(tmp_path, monkeypatch)
    provider.context.oauth_metadata = None
    provider._hermes_transport_options = {
        "connect_timeout": 7,
        "ssl_verify": "C:/HERMES-TEMP/custom-ca.pem",
        "client_cert": ("C:/HERMES-TEMP/client.pem", "C:/HERMES-TEMP/client.key"),
    }
    provider._hermes_control_plane_required = True
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def send(self, request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "resource": "https://example.com/mcp",
                        "authorization_servers": ["https://auth.example"],
                    },
                )
            return httpx.Response(
                200,
                request=request,
                json={"token_endpoint": "https://auth.example/token"},
            )

    client_kwargs = {}

    def capture_client(**kwargs):
        client_kwargs.update(kwargs)
        return FakeClient(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", capture_client)
    monkeypatch.setattr(
        auth_utils,
        "build_protected_resource_metadata_discovery_urls",
        lambda *_: ["https://example.com/.well-known/oauth-protected-resource"],
    )
    monkeypatch.setattr(
        auth_utils,
        "build_oauth_authorization_server_metadata_discovery_urls",
        lambda *_: ["https://auth.example/.well-known/oauth-authorization-server"],
    )

    async def handle_prm(_response):
        return _metadata_prm()

    async def handle_asm(_response):
        return True, _metadata_asm()

    monkeypatch.setattr(auth_utils, "handle_protected_resource_response", handle_prm)
    monkeypatch.setattr(auth_utils, "handle_auth_metadata_response", handle_asm)
    await provider._prefetch_oauth_metadata()
    assert client_kwargs["verify"] == "C:/HERMES-TEMP/custom-ca.pem"
    assert client_kwargs["cert"] == (
        "C:/HERMES-TEMP/client.pem",
        "C:/HERMES-TEMP/client.key",
    )
    assert client_kwargs["timeout"].connect == 7
    assert all("authorization" not in request.headers for request in requests)
    assert str(requests[0].url).endswith("oauth-protected-resource")
    assert str(requests[1].url).endswith("oauth-authorization-server")


@pytest.mark.asyncio
async def test_strict_redirect_client_uses_current_request_origin_for_control_redirect(
    monkeypatch,
):
    """A control request on another origin keeps headers on its own redirect."""
    from tools.mcp_oauth_manager import _StrictRedirectAsyncClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == "https://auth.example/metadata":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://auth.example/metadata-next"},
            )
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)
    client = _StrictRedirectAsyncClient(
        follow_redirects=True,
        headers={"X-Strict-Secret": "must-stay-on-auth-origin"},
        redirect_origin=httpx.URL("https://mcp.example/mcp"),
        configured_header_names={"x-strict-secret"},
    )
    async with client:
        response = await client.get("https://auth.example/metadata")

    assert response.status_code == 200
    assert len(seen) == 2
    assert all(
        request.headers["x-strict-secret"] == "must-stay-on-auth-origin"
        for request in seen
    )


@pytest.mark.asyncio
async def test_public_current_http_real_followed_redirect_strips_strict_headers(
    monkeypatch,
):
    """The public current client must enforce strict policy on HTTPX redirects."""
    import tools.mcp_tool as tool_module

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == "https://source.example/mcp":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://source.example/next"},
            )
        if str(request.url) == "https://source.example/next":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://other.example/next"},
            )
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return SimpleNamespace(capabilities=SimpleNamespace(tools=None))

    @asynccontextmanager
    async def stream_factory(url, *, http_client, **_kwargs):
        await http_client.get(url)
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(tool_module, "ClientSession", FakeSession)
    monkeypatch.setattr(tool_module, "streamable_http_client", stream_factory)
    monkeypatch.setattr(tool_module, "streamablehttp_client", stream_factory)
    monkeypatch.setattr(tool_module, "_MCP_NEW_HTTP", True)
    monkeypatch.setattr(
        tool_module.MCPServerTask, "_discover_tools", lambda self: _done()
    )
    monkeypatch.setattr(
        tool_module.MCPServerTask,
        "_wait_for_lifecycle_event",
        lambda self: _shutdown(),
    )
    monkeypatch.setattr(tool_module, "_reset_server_error", lambda _name: None)

    task = tool_module.MCPServerTask("strict-redirect")
    task._auth_type = ""
    await task._run_http({
        "url": "https://source.example/mcp",
        "transport": "streamable_http",
        "strict_redirect_headers": True,
        "headers": {
            "Authorization": "Bearer resource-secret",
            "X-Strict-Secret": "must-not-cross-origin",
        },
        "identity_header": {
            "name": "X-Resolved-Identity",
            "value": "alice",
        },
    })

    assert len(seen) == 3
    assert seen[0].headers["authorization"] == "Bearer resource-secret"
    assert seen[0].headers["x-strict-secret"] == "must-not-cross-origin"
    assert seen[1].headers["authorization"] == "Bearer resource-secret"
    assert seen[1].headers["x-strict-secret"] == "must-not-cross-origin"
    assert seen[0].headers["x-resolved-identity"] == "alice"
    assert seen[1].headers["x-resolved-identity"] == "alice"
    assert "authorization" not in seen[2].headers
    assert "x-strict-secret" not in seen[2].headers
    assert "x-resolved-identity" not in seen[2].headers


@pytest.mark.asyncio
async def test_cold_prefetch_real_followed_redirect_strips_strict_headers(
    tmp_path, monkeypatch
):
    """Cold PRM/ASM prefetch must enforce the same cross-origin policy."""
    provider = await _provider(tmp_path, monkeypatch)
    provider.context.oauth_metadata = None
    provider._hermes_transport_options = {
        "headers": {"X-Strict-Secret": "must-not-cross-origin"},
        "strict_redirect_headers": True,
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url == "https://example.com/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://attacker.example/prm"},
            )
        if url == "https://attacker.example/prm":
            return httpx.Response(
                200,
                request=request,
                json={
                    "resource": "https://example.com/mcp",
                    "authorization_servers": ["https://auth.example"],
                },
            )
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://attacker.example/asm"},
            )
        if url == "https://attacker.example/asm":
            return httpx.Response(
                200,
                request=request,
                json={
                    "issuer": "https://auth.example",
                    "authorization_endpoint": "https://auth.example/authorize",
                    "token_endpoint": "https://auth.example/token",
                    "response_types_supported": ["code"],
                },
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)
    await provider._prefetch_oauth_metadata()

    assert [str(request.url) for request in seen] == [
        "https://example.com/.well-known/oauth-protected-resource/mcp",
        "https://attacker.example/prm",
        "https://auth.example/.well-known/oauth-authorization-server",
        "https://attacker.example/asm",
    ]
    assert seen[0].headers["x-strict-secret"] == "must-not-cross-origin"
    assert "x-strict-secret" not in seen[1].headers
    assert seen[2].headers["x-strict-secret"] == "must-not-cross-origin"
    assert "x-strict-secret" not in seen[3].headers


def _metadata_prm():
    return SimpleNamespace(
        resource="https://example.com/mcp",
        authorization_servers=["https://auth.example"],
    )


def _metadata_asm():
    class Metadata:
        token_endpoint = "https://auth.example/token"

        def model_dump(self, **_kwargs):
            return {"token_endpoint": self.token_endpoint}

    return Metadata()


@pytest.mark.parametrize("transport", ["sse", "current", "legacy"])
@pytest.mark.parametrize("auth_type", ["", "oauth"])
@pytest.mark.asyncio
async def test_public_run_http_transport_runtime_matrix(
    transport, auth_type, monkeypatch
):
    import tools.mcp_tool as tool_module

    events = []
    calls = []
    provider = object()

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        async def __aenter__(self):
            events.append("client-enter")
            return self

        async def __aexit__(self, *_args):
            events.append("client-exit")

    class FakeSession:
        def __init__(self, *_args, **kwargs):
            calls.append(("session", kwargs))

        async def __aenter__(self):
            events.append("session-enter")
            return self

        async def __aexit__(self, *_args):
            events.append("session-exit")

        async def initialize(self):
            return SimpleNamespace(capabilities=SimpleNamespace(tools=None))

    @asynccontextmanager
    async def stream_factory(*args, **kwargs):
        calls.append(("stream", args, kwargs))
        events.append("stream-enter")
        yield (object(), object(), lambda: None)
        events.append("stream-exit")

    @asynccontextmanager
    async def sse_factory(*args, **kwargs):
        calls.append(("sse", args, kwargs))
        events.append("sse-enter")
        factory = kwargs.get("httpx_client_factory")
        if factory is not None:
            client = factory(
                headers={}, timeout=1.0, auth=provider if auth_type else None
            )
            await client.__aenter__()
            await client.__aexit__(None, None, None)
        yield (object(), object())
        events.append("sse-exit")

    monkeypatch.setattr(tool_module, "ClientSession", FakeSession)
    monkeypatch.setattr(
        tool_module.MCPServerTask, "_discover_tools", lambda self: _done()
    )
    monkeypatch.setattr(
        tool_module.MCPServerTask,
        "_wait_for_lifecycle_event",
        lambda self: _shutdown(),
    )
    monkeypatch.setattr(tool_module, "_reset_server_error", lambda _name: None)
    monkeypatch.setattr(tool_module, "sse_client", sse_factory)
    monkeypatch.setattr(tool_module, "streamable_http_client", stream_factory)
    monkeypatch.setattr(tool_module, "streamablehttp_client", stream_factory)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(tool_module, "_MCP_NEW_HTTP", transport == "current")

    class Manager:
        def get_or_build_provider(self, *_args, **_kwargs):
            calls.append(("provider", _args, _kwargs))
            return provider

    monkeypatch.setattr("tools.mcp_oauth_manager.get_manager", lambda: Manager())
    task = tool_module.MCPServerTask("srv")
    task._auth_type = auth_type
    task._config = {"keepalive_interval": 999}
    config = {
        "url": "https://example.com/mcp",
        "transport": "sse" if transport == "sse" else "streamable_http",
        "connect_timeout": 2,
        "ssl_verify": False,
        "headers": {"X-Test": "yes"},
        "oauth": {"scope": "read"},
    }
    await task._run_http(config)
    assert any(item[0] == transport_map(transport) for item in calls)
    provider_calls = [item for item in calls if item[0] == "provider"]
    assert bool(provider_calls) is bool(auth_type)
    if auth_type:
        branch = next(item for item in calls if item[0] == transport_map(transport))
        kwargs = branch[-1]
        if transport == "current":
            assert kwargs["auth"] is provider
            assert kwargs["verify"] is False
            assert kwargs["timeout"].connect == 2.0
        elif transport == "legacy":
            assert kwargs["auth"] is provider
            assert callable(kwargs["httpx_client_factory"])
            legacy_client = kwargs["httpx_client_factory"](
                headers=kwargs["headers"],
                timeout=kwargs["timeout"],
                auth=kwargs["auth"],
            )
            legacy_client_kwargs = next(
                item[1] for item in calls if item[0] == "client"
            )
            assert legacy_client is not None
            assert legacy_client_kwargs["verify"] is False
            assert legacy_client_kwargs["timeout"] == 2.0
        else:
            client_kwargs = next(item[1] for item in calls if item[0] == "client")
            assert client_kwargs["verify"] is False
            assert client_kwargs["timeout"] == 1.0
            assert kwargs["timeout"] == 2.0
            assert kwargs["sse_read_timeout"] == 300.0
    assert "session-exit" in events


@pytest.mark.asyncio
async def test_public_403_insufficient_scope_step_up_persists_and_retries(
    tmp_path, monkeypatch
):
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.shared.auth import OAuthToken

    provider = await _provider(tmp_path, monkeypatch)
    auth_calls = 0

    async def perform_authorization(_self):
        nonlocal auth_calls
        auth_calls += 1
        return httpx.Request("POST", "https://idp.example/token")

    async def handle_token_response(_self, _response):
        token = OAuthToken(
            access_token="stepped",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="rotated",
            scope="read write",
        )
        _self.context.current_tokens = token
        _self.context.update_token_expiry(token)
        await _self.context.storage.set_tokens(token)

    monkeypatch.setattr(
        OAuthClientProvider, "_perform_authorization", perform_authorization
    )
    monkeypatch.setattr(
        OAuthClientProvider, "_handle_token_response", handle_token_response
    )
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    original = await flow.__anext__()
    forbidden = httpx.Response(
        403,
        request=original,
        headers={
            "www-authenticate": 'Bearer error="insufficient_scope", scope="write"'
        },
    )
    token_request = await flow.asend(forbidden)
    assert str(token_request.url) == "https://idp.example/token"
    assert "authorization" not in token_request.headers
    retry = await flow.asend(httpx.Response(200, request=token_request))
    assert retry.headers["authorization"] == "Bearer stepped"
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200, request=retry))
    assert auth_calls == 1
    saved = await provider.context.storage.get_tokens()
    assert saved is not None and saved.access_token == "stepped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "challenge",
    [
        'Bearer error="invalid_token"',
        'Bearer error="insufficient_scope"',
        'Bearer error="insufficient_scope", scope="',
    ],
)
async def test_public_403_non_step_up_does_not_authorize(
    challenge, tmp_path, monkeypatch
):
    from mcp.client.auth.oauth2 import OAuthClientProvider

    provider = await _provider(tmp_path, monkeypatch)
    auth_calls = 0

    async def perform_authorization(_self):
        nonlocal auth_calls
        auth_calls += 1
        raise AssertionError("negative 403 must not authorize")

    monkeypatch.setattr(
        OAuthClientProvider, "_perform_authorization", perform_authorization
    )
    flow = provider.async_auth_flow(httpx.Request("POST", "https://example.com/mcp"))
    original = await flow.__anext__()
    retry = await flow.asend(
        httpx.Response(403, request=original, headers={"www-authenticate": challenge})
    )
    assert retry.headers["authorization"] == "Bearer access"
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200, request=retry))
    assert auth_calls == 0


def transport_map(value):
    return {"sse": "sse", "current": "client", "legacy": "stream"}[value]


async def _done():
    return None


async def _shutdown():
    return "shutdown"
