"""Fail-closed and sequencing tests for MCP OAuth token rotation."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest


pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK 2.0+ required")

from mcp.shared.auth import (  # noqa: E402
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)
from pydantic import AnyUrl  # noqa: E402

from tools.mcp_oauth import HermesTokenStorage  # noqa: E402
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS  # noqa: E402
from tools.mcp_tool import sdk_httpx  # noqa: E402


async def _seed_storage(
    storage: HermesTokenStorage,
    *,
    refresh_token: str = "single-use-refresh",
    scope: str | None = "files:read",
) -> None:
    await storage.set_tokens(
        OAuthToken(
            access_token="expired-access",
            token_type="Bearer",
            expires_in=0,
            refresh_token=refresh_token,
            scope=scope,
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )
    storage.save_oauth_metadata(
        OAuthMetadata.model_validate(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "response_types_supported": ["code"],
            }
        )
    )


def _build_provider(storage: HermesTokenStorage, browser_calls: list[str] | None = None):
    assert _HERMES_PROVIDER_CLS is not None
    calls = browser_calls if browser_calls is not None else []

    async def _redirect(url: str) -> None:
        calls.append(url)

    async def _callback() -> tuple[str, str | None]:
        raise AssertionError("browser authorization must not run")

    return _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://mcp.example.com/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            client_name="Hermes Agent test",
        ),
        storage=storage,
        redirect_handler=_redirect,
        callback_handler=_callback,
    )


async def _finish_mcp_request(flow, request, httpx) -> None:
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200, request=request))


@pytest.mark.asyncio
async def test_storage_initialization_runs_while_sdk_context_lock_is_held(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("srv")
    await _seed_storage(storage)
    provider = _build_provider(storage)
    original_initialize = provider._initialize

    async def _guarded_initialize() -> None:
        assert provider.context.lock.locked()
        await original_initialize()

    provider._initialize = _guarded_initialize
    httpx = sdk_httpx()
    flow = provider.async_auth_flow(httpx.Request("POST", "https://mcp.example.com/mcp"))
    refresh_request = await flow.__anext__()
    mcp_request = await flow.asend(
        httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "fresh-refresh",
            },
            request=refresh_request,
        )
    )
    await _finish_mcp_request(flow, mcp_request, httpx)


@pytest.mark.asyncio
async def test_rotating_refresh_persistence_failure_blocks_stale_token_reuse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("srv")
    await _seed_storage(storage)
    provider = _build_provider(storage)
    httpx = sdk_httpx()
    flow = provider.async_auth_flow(httpx.Request("POST", "https://mcp.example.com/mcp"))
    refresh_request = await flow.__anext__()

    async def _fail_persist(_tokens: OAuthToken) -> None:
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(storage, "set_tokens", _fail_persist)
    with pytest.raises(Exception, match="persist"):
        await flow.asend(
            httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "rotated-refresh",
                },
                request=refresh_request,
            )
        )

    second = _build_provider(HermesTokenStorage("srv"))
    second_flow = second.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    with pytest.raises(Exception, match="explicit reauthorization"):
        await second_flow.__anext__()


@pytest.mark.asyncio
async def test_refresh_response_carries_forward_omitted_refresh_token_and_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("srv")
    await _seed_storage(storage, refresh_token="carry-refresh", scope="files:read")
    provider = _build_provider(storage)
    httpx = sdk_httpx()
    flow = provider.async_auth_flow(httpx.Request("POST", "https://mcp.example.com/mcp"))
    refresh_request = await flow.__anext__()
    mcp_request = await flow.asend(
        httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            request=refresh_request,
        )
    )

    saved = await storage.get_tokens()
    assert saved is not None
    assert saved.refresh_token == "carry-refresh"
    assert saved.scope == "files:read"
    await _finish_mcp_request(flow, mcp_request, httpx)


@pytest.mark.asyncio
async def test_closing_refresh_flow_releases_sdk_lock_and_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("srv")
    await _seed_storage(storage)
    provider = _build_provider(storage)
    httpx = sdk_httpx()

    first_flow = provider.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    await first_flow.__anext__()
    await first_flow.aclose()

    second_flow = provider.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    with pytest.raises(Exception, match="explicit reauthorization"):
        await asyncio.wait_for(second_flow.__anext__(), timeout=2)


def test_public_builder_uses_singleflight_provider(tmp_path, monkeypatch):
    from tools.mcp_oauth import build_oauth_auth
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.mcp_oauth._is_interactive", lambda: True)
    reset_manager_for_tests()

    provider = build_oauth_auth("srv", "https://mcp.example.com/mcp", {})

    assert _HERMES_PROVIDER_CLS is not None
    assert isinstance(provider, _HERMES_PROVIDER_CLS)


@pytest.mark.asyncio
async def test_outer_close_releases_lease_even_when_inner_close_fails(
    tmp_path, monkeypatch
):
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests
    from tools.mcp_tool import sdk_httpx

    assert _HERMES_PROVIDER_CLS is not None
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_manager_for_tests()
    httpx = sdk_httpx()

    class ProbeLease:
        def __init__(self):
            self.released = 0

        async def release(self):
            self.released += 1

    async def failing_inner(_self, request):
        try:
            yield request
        finally:
            raise RuntimeError("simulated inner close failure")

    base_provider = _HERMES_PROVIDER_CLS.__mro__[1]
    monkeypatch.setattr(base_provider, "async_auth_flow", failing_inner)

    provider = _HERMES_PROVIDER_CLS.__new__(_HERMES_PROVIDER_CLS)
    provider._hermes_server_name = "srv"
    provider._hermes_home = str(tmp_path)
    lease = ProbeLease()
    provider._hermes_refresh_lease = lease
    flow = provider.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )

    await flow.__anext__()
    with pytest.raises(RuntimeError, match="simulated inner close failure"):
        await flow.aclose()

    assert lease.released == 1


def test_cli_reauthorization_holds_refresh_lock_before_mutation(monkeypatch):
    from hermes_cli import mcp_config
    from tools import mcp_oauth

    events: list[str] = []
    locked = False

    class Lock:
        def __enter__(self):
            nonlocal locked
            locked = True
            events.append("locked")

        def __exit__(self, *_exc):
            nonlocal locked
            locked = False
            events.append("released")

    class Storage:
        def __init__(self, _name):
            pass

        def refresh_lock(self):
            return Lock()

    def mutate(_name, _config):
        assert locked
        events.append("mutated")
        return True

    monkeypatch.setattr(mcp_oauth, "HermesTokenStorage", Storage)
    monkeypatch.setattr(mcp_config, "_reauth_oauth_server_under_lock", mutate)

    assert mcp_config._reauth_oauth_server("srv", {"auth": "oauth"}) is True
    assert events == ["locked", "mutated", "released"]


def test_dashboard_reauthorization_holds_refresh_lock_before_mutation(
    tmp_path, monkeypatch
):
    from agent import secret_scope
    from hermes_cli import mcp_config, web_server
    import hermes_constants
    from tools import mcp_dashboard_oauth, mcp_oauth, mcp_oauth_manager

    events: list[str] = []
    locked = False

    class Lock:
        def __enter__(self):
            nonlocal locked
            locked = True
            events.append("locked")

        def __exit__(self, *_exc):
            nonlocal locked
            locked = False
            events.append("released")

    class Storage:
        def __init__(self, _name):
            pass

        def refresh_lock(self):
            return Lock()

        def snapshot(self):
            assert locked
            events.append("snapshot")
            return {}

        def restore(self, *_args, **_kwargs):
            assert locked

    class Manager:
        def remove(self, *_args, **_kwargs):
            assert locked
            events.append("mutated")
            raise RuntimeError("stop after mutation probe")

        def restore_entry(self, *_args, **_kwargs):
            assert locked

    class Flow:
        hermes_home = str(tmp_path)
        server_name = "srv"
        reconnect_live = False

        def mark_error(self, _message):
            events.append("error")

        def mark_worker_done(self):
            events.append("done")

    monkeypatch.setattr(mcp_oauth, "HermesTokenStorage", Storage)
    monkeypatch.setattr(mcp_oauth, "force_interactive_oauth", nullcontext)
    monkeypatch.setattr(mcp_oauth_manager, "get_manager", lambda: Manager())
    monkeypatch.setattr(mcp_dashboard_oauth, "dashboard_oauth_flow", lambda _flow: nullcontext())
    monkeypatch.setattr(web_server, "_mcp_oauth_transaction", lambda _flow: nullcontext())
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda *_a, **_kw: [])
    monkeypatch.setattr(hermes_constants, "set_hermes_home_override", lambda _home: object())
    monkeypatch.setattr(hermes_constants, "reset_hermes_home_override", lambda _token: None)
    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", lambda _home: object())
    monkeypatch.setattr(secret_scope, "set_secret_scope", lambda _scope: object())
    monkeypatch.setattr(secret_scope, "reset_secret_scope", lambda _token: None)

    web_server._run_dashboard_mcp_oauth(Flow(), {"url": "https://mcp.example.com"})

    assert events[:4] == ["locked", "snapshot", "mutated", "released"]
    assert locked is False


def test_tui_reauthorization_holds_refresh_lock_before_mutation(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_cli import mcp_config
    import hermes_constants
    from tools import mcp_dashboard_oauth, mcp_oauth, mcp_oauth_manager
    from tui_gateway import mcp_oauth_sessions

    events: list[str] = []
    locked = False

    class Lock:
        def __enter__(self):
            nonlocal locked
            locked = True
            events.append("locked")

        def __exit__(self, *_exc):
            nonlocal locked
            locked = False
            events.append("released")

    class Storage:
        def __init__(self, _name):
            pass

        def refresh_lock(self):
            return Lock()

        def snapshot(self):
            assert locked
            events.append("snapshot")
            return {}

        def restore(self, *_args, **_kwargs):
            assert locked

    class Manager:
        def remove(self, *_args, **_kwargs):
            assert locked
            events.append("mutated")
            raise RuntimeError("stop after mutation probe")

        def restore_entry(self, *_args, **_kwargs):
            assert locked

    class Flow:
        def mark_error(self, _message):
            events.append("error")

        def mark_worker_done(self):
            events.append("done")

    flow = Flow()
    mcp_oauth_sessions._sessions["test-session"] = {"flow": flow}
    monkeypatch.setattr(mcp_oauth, "HermesTokenStorage", Storage)
    monkeypatch.setattr(mcp_oauth, "force_interactive_oauth", nullcontext)
    monkeypatch.setattr(mcp_oauth_manager, "get_manager", lambda: Manager())
    monkeypatch.setattr(mcp_dashboard_oauth, "dashboard_oauth_flow", lambda _flow: nullcontext())
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda *_a, **_kw: [])
    monkeypatch.setattr(hermes_constants, "set_hermes_home_override", lambda _home: object())
    monkeypatch.setattr(hermes_constants, "reset_hermes_home_override", lambda _token: None)
    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", lambda _home: object())
    monkeypatch.setattr(secret_scope, "set_secret_scope", lambda _scope: object())
    monkeypatch.setattr(secret_scope, "reset_secret_scope", lambda _token: None)
    monkeypatch.setattr(mcp_oauth_sessions, "_shutdown_listener", lambda _rec: None)

    try:
        mcp_oauth_sessions._worker(
            "test-session",
            str(tmp_path),
            "srv",
            {"url": "https://mcp.example.com"},
            False,
        )
    finally:
        mcp_oauth_sessions._sessions.pop("test-session", None)

    assert events[:4] == ["locked", "snapshot", "mutated", "released"]
    assert locked is False


@pytest.mark.parametrize(
    "marker_text",
    [
        "{not-json",
        '{"version": 2, "status": "in_flight", "refresh_token_sha256": "' + "a" * 64 + '"}',
        '{"version": true, "status": "in_flight", "refresh_token_sha256": "' + "a" * 64 + '"}',
        '{"version": 1, "status": "unknown", "refresh_token_sha256": "' + "a" * 64 + '"}',
        '{"version": 1, "status": [], "refresh_token_sha256": "' + "a" * 64 + '"}',
        '{"version": 1, "status": "in_flight"}',
        '{"version": 1, "status": "in_flight", "refresh_token_sha256": "short"}',
    ],
)
def test_corrupt_consumed_generation_marker_fails_closed(
    tmp_path,
    monkeypatch,
    marker_text,
):
    """An unreadable claim marker must never authorize reuse of a token."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_oauth import HermesTokenStorage

    storage = HermesTokenStorage("srv")
    marker = storage._refresh_state_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(marker_text, encoding="utf-8")

    assert storage.claim_refresh_token("single-use-refresh") is False
