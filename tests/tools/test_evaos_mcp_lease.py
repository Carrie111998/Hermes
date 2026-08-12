"""Managed evaOS MCP lease authentication and refresh behavior."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tools.evaos_mcp_lease import (
    EvaosLeaseError,
    EvaosLeaseHttpAuth,
    EvaosLeaseManager,
    EvaosLeaseSource,
)
from tools.mcp_schema_cache import config_fingerprint
from tools.mcp_tool import MCPServerTask

class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _write_secret(path, value: str):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _source(tmp_path, *, app_slug="google_sheets", profile_key="profile-a"):
    broker = tmp_path / "broker"
    _write_secret(broker, "broker-secret-under-test\n")
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": str(broker),
    }
    source = EvaosLeaseSource(
        profile_key=profile_key,
        app_slug=app_slug,
        secret_reader=values.get,
        profile_resolver=lambda: profile_key,
        root_uid=os.getuid(),
    )
    return source, broker


def _lease_payload(expires_at: datetime, token="lease-token-1"):
    return {
        "mcp_url": "https://remote.mcp.pipedream.net/v3",
        "headers": {
            "Authorization": f"Bearer {token}",
            "x-pd-project-id": "proj",
            "x-pd-environment": "production",
            "x-pd-external-user-id": "server-derived",
            "x-pd-app-slug": "google_sheets",
            "x-pd-account-id": "apn_test_server_resolved",
        },
        "expires_at": expires_at.isoformat(),
    }


def test_source_defaults_service_uid_to_root_uid_without_geteuid(
    monkeypatch,
):
    monkeypatch.delattr(os, "geteuid", raising=False)

    source = EvaosLeaseSource(
        profile_key="profile-a",
        app_slug="google_sheets",
        secret_reader=lambda _name: None,
        profile_resolver=lambda: "profile-a",
        root_uid=4321,
    )

    assert source._service_uid == 4321


@pytest.mark.asyncio
async def test_lease_request_uses_only_root_configured_profile_route(tmp_path):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _ = _source(tmp_path)
    calls = []
    settings_read = []
    original_reader = source._secret_reader
    source._secret_reader = lambda name: (
        settings_read.append(name) or original_reader(name)
    )

    async def transport(url, headers, payload):
        calls.append((url, headers, payload))
        return _Response(200, _lease_payload(now + timedelta(minutes=10)))

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: now,
    )
    lease = await manager.get_lease()

    assert lease.mcp_url == "https://remote.mcp.pipedream.net/v3"
    assert calls[0][2] == {
        "action": "pipedream_mcp_lease",
        "app_slug": "google_sheets",
    }
    # adapter#89: no per-app grant handle is sent, and the grant-file setting is
    # never even looked up. Pipedream Connect MCP requires no such handle, and
    # the compat edge fn derives x-pd-external-user-id server-side.
    assert "X-Evaos-Provider-Grant" not in calls[0][1]
    assert "PIPEDREAM_PROVIDER_GRANT_FILE" not in settings_read
    assert not {
        "account_id",
        "profile",
        "profile_id",
        "external_user_id",
        "project_id",
        "environment",
    } & calls[0][2].keys()


@pytest.mark.asyncio
async def test_expiry_refreshes_before_skew(tmp_path):
    clock = [datetime(2026, 8, 8, tzinfo=timezone.utc)]
    source, _ = _source(tmp_path)
    calls = 0

    async def transport(url, headers, payload):
        nonlocal calls
        calls += 1
        return _Response(
            200,
            _lease_payload(
                clock[0] + timedelta(seconds=90),
                token=f"lease-token-{calls}",
            ),
        )

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: clock[0],
        refresh_skew_seconds=60,
    )
    first = await manager.get_lease()
    clock[0] += timedelta(seconds=31)
    second = await manager.get_lease()

    assert first.authorization != second.authorization
    assert calls == 2


@pytest.mark.asyncio
async def test_concurrent_refresh_is_single_flight(tmp_path):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _ = _source(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def transport(url, headers, payload):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _Response(200, _lease_payload(now + timedelta(minutes=10)))

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: now,
    )
    tasks = [asyncio.create_task(manager.get_lease()) for _ in range(8)]
    await entered.wait()
    release.set()
    leases = await asyncio.gather(*tasks)

    assert calls == 1
    assert len({lease.authorization for lease in leases}) == 1


@pytest.mark.asyncio
async def test_http_auth_refreshes_and_retries_exactly_once_after_401(tmp_path):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _ = _source(tmp_path)
    calls = 0

    async def transport(url, headers, payload):
        nonlocal calls
        calls += 1
        assert "X-Evaos-Provider-Grant" not in headers
        return _Response(
            200,
            _lease_payload(
                now + timedelta(minutes=10),
                token=f"lease-token-{calls}",
            ),
        )

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: now,
    )
    auth = EvaosLeaseHttpAuth(manager)
    request = httpx.Request(
        "POST",
        "https://remote.mcp.pipedream.net/v3",
        json={"jsonrpc": "2.0"},
    )
    flow = auth.async_auth_flow(request)

    first = await anext(flow)
    assert first.headers["Authorization"] == "Bearer lease-token-1"
    second = await flow.asend(httpx.Response(401, request=first))
    assert second.headers["Authorization"] == "Bearer lease-token-2"
    assert calls == 2
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(401, request=second))
    assert calls == 2


def test_source_rejects_cross_profile_and_unsafe_files(tmp_path):
    source, broker = _source(tmp_path)
    source._profile_resolver = lambda: "profile-b"
    with pytest.raises(EvaosLeaseError, match="profile authority"):
        source.read()

    source._profile_resolver = lambda: "profile-a"
    broker.chmod(0o644)
    with pytest.raises(EvaosLeaseError, match="secure managed credential"):
        source.read()


@pytest.mark.parametrize("mode", [0o400, 0o440])
def test_source_accepts_systemd_loadcredential_copy(
    tmp_path,
    monkeypatch,
    mode,
):
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    broker = credentials / "pipedream_broker"
    _write_secret(broker, "broker-secret-under-test\n")
    broker.chmod(mode)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": str(broker),
    }
    source = EvaosLeaseSource(
        profile_key="profile-a",
        app_slug="google_sheets",
        secret_reader=values.get,
        profile_resolver=lambda: "profile-a",
        root_uid=os.getuid(),
        service_uid=os.getuid(),
    )

    material = source.read()

    assert material.app_slug == "google_sheets"


def test_source_accepts_service_owned_0400_systemd_copy(
    tmp_path,
    monkeypatch,
):
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    broker = credentials / "pipedream_broker"
    _write_secret(broker, "broker-secret-under-test\n")
    broker.chmod(0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": "%d/pipedream_broker",
    }
    source = EvaosLeaseSource(
        profile_key="profile-a",
        app_slug="google_sheets",
        secret_reader=values.get,
        profile_resolver=lambda: "profile-a",
        root_uid=os.getuid() + 1000,
        service_uid=os.getuid(),
    )

    material = source.read()

    assert material.app_slug == "google_sheets"


def test_source_rejects_0400_outside_systemd_credential_directory(tmp_path):
    source, broker = _source(tmp_path)
    broker.chmod(0o400)

    with pytest.raises(EvaosLeaseError, match="secure managed credential"):
        source.read()


def test_source_rejects_unsafe_systemd_credential_pointer(tmp_path, monkeypatch):
    source, _ = _source(tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    original_reader = source._secret_reader
    source._secret_reader = lambda name: (
        "%d/../broker-secret"
        if name == "PIPEDREAM_AGENT_BROKER_SECRET_FILE"
        else original_reader(name)
    )

    with pytest.raises(EvaosLeaseError, match="systemd credential pointer"):
        source.read()


def test_source_rejects_symlinked_secret_file(tmp_path):
    source, broker = _source(tmp_path)
    target = tmp_path / "broker-target"
    broker.rename(target)
    broker.symlink_to(target)

    with pytest.raises(EvaosLeaseError, match="broker secret file"):
        source.read()


def test_runtime_endpoint_and_broker_path_stay_global(monkeypatch):
    from agent import secret_scope

    monkeypatch.setenv(
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL",
        "https://global.example/functions/v1/desktop-runtime-session",
    )
    monkeypatch.setenv(
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE",
        "/global/broker",
    )
    monkeypatch.setenv(
        "CREDENTIALS_DIRECTORY",
        "/run/credentials/evaos-shared-gateway.service",
    )
    token = secret_scope.set_secret_scope(
        {
            "CREDENTIALS_DIRECTORY": "/wrong/profile/credentials",
        }
    )
    secret_scope.set_multiplex_active(True)
    try:
        assert secret_scope.get_secret(
            "EVAOS_DESKTOP_RUNTIME_SESSION_URL"
        ).startswith("https://global.example/")
        assert secret_scope.get_secret(
            "PIPEDREAM_AGENT_BROKER_SECRET_FILE"
        ) == "/global/broker"
        assert secret_scope.get_secret("CREDENTIALS_DIRECTORY") == (
            "/run/credentials/evaos-shared-gateway.service"
        )
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


def test_source_rejects_non_supabase_lease_endpoint(tmp_path):
    source, _ = _source(tmp_path)
    original_reader = source._secret_reader
    source._secret_reader = lambda name: (
        "https://attacker.example/functions/v1/desktop-runtime-session"
        if name == "EVAOS_DESKTOP_RUNTIME_SESSION_URL"
        else original_reader(name)
    )

    with pytest.raises(EvaosLeaseError, match="lease endpoint"):
        source.read()


@pytest.mark.asyncio
async def test_errors_and_repr_never_expose_tokens(tmp_path, caplog):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _ = _source(tmp_path)
    secret = "server-body-secret-that-must-not-escape"

    async def transport(url, headers, payload):
        return _Response(403, {"error": secret})

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: now,
    )
    with pytest.raises(EvaosLeaseError) as caught:
        await manager.get_lease()

    rendered = str(caught.value) + caplog.text + repr(manager)
    assert secret not in rendered
    assert "broker-secret-under-test" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload, now: payload.update(
            mcp_url="https://attacker.example/v3"
        ),
        lambda payload, now: payload["headers"].update(
            {"x-extra-credential": "must-not-be-forwarded"}
        ),
        lambda payload, now: payload["headers"].update(
            {"x-pd-app-slug": "google_drive"}
        ),
        lambda payload, now: payload["headers"].update(
            {"x-pd-account-id": "legacy_account"}
        ),
        lambda payload, now: payload["headers"].update(
            {"Authorization": "Bearer injected\r\nX-Leak: value"}
        ),
        lambda payload, now: payload.update(
            expires_at=(now + timedelta(seconds=30)).isoformat()
        ),
        lambda payload, now: payload.update(extra="unexpected"),
    ],
)
async def test_lease_response_is_strictly_validated(tmp_path, mutate):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _ = _source(tmp_path)
    payload = _lease_payload(now + timedelta(minutes=10))
    mutate(payload, now)

    async def transport(url, headers, request_payload):
        return _Response(200, payload)

    manager = EvaosLeaseManager(
        source=source,
        transport=transport,
        now=lambda: now,
    )

    with pytest.raises(EvaosLeaseError, match="lease response|expires too soon"):
        await manager.get_lease()


def test_managed_lease_config_is_http_without_a_static_url():
    task = MCPServerTask("pipedream-google-sheets")
    task._config = {
        "auth": "evaos_lease",
        "app_slug": "google_sheets",
        "lazy": True,
    }
    task._auth_type = "evaos_lease"

    task._validate_evaos_lease_config(task._config)

    assert task._is_http() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {},
        {
            "customer_id": "customer-fixture",
            "agent_runtime": "hermes",
            "agent_id": "agent-fixture",
        },
    ],
    ids=["thin", "identity-keyed"],
)
async def test_managed_config_mounts_through_r5_lease(
    tmp_path, monkeypatch, identity
):
    from tools import evaos_mcp_lease as lease_module
    from tools import mcp_tool as mcp_tool_module

    profile = tmp_path / "profile-test"
    credentials = tmp_path / "credentials"
    profile.mkdir()
    credentials.mkdir(mode=0o700)
    broker = credentials / "pipedream_broker"
    _write_secret(broker, "broker-secret-under-test\n")
    broker.chmod(0o400)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv(
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL",
        "https://example.supabase.co/functions/v1/desktop-runtime-session",
    )
    monkeypatch.setenv("PIPEDREAM_AGENT_BROKER_SECRET_FILE", str(broker))
    # adapter#89 regression: NO grant file exists and no grant setting is set.
    monkeypatch.delenv("PIPEDREAM_PROVIDER_GRANT_FILE", raising=False)
    assert not (credentials / "pipedream_grants").exists()

    mint_calls = []

    async def mint(url, headers, payload):
        mint_calls.append((url, headers, payload))
        return _Response(
            200,
            _lease_payload(
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ),
        )

    monkeypatch.setattr(lease_module, "_default_transport", mint)

    mounted = {}

    class Mounted(Exception):
        pass

    class CaptureTransport:
        async def __aenter__(self):
            raise Mounted

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    def capture_transport(url, **kwargs):
        mounted.update(url=url, **kwargs)
        return CaptureTransport()

    monkeypatch.setattr(mcp_tool_module, "_MCP_HTTP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool_module, "_MCP_NEW_HTTP", False)
    monkeypatch.setattr(
        mcp_tool_module, "streamablehttp_client", capture_transport
    )

    config = {
        "auth": "evaos_lease",
        "app_slug": "google_sheets",
        "lazy": True,
        **identity,
    }
    task = MCPServerTask("pipedream-google-sheets", str(profile))
    task._auth_type = "evaos_lease"
    task._validate_evaos_lease_config(config)

    with pytest.raises(Mounted):
        await task._run_http(config)

    assert mint_calls[0][2] == {
        "action": "pipedream_mcp_lease",
        "app_slug": "google_sheets",
    }
    assert "X-Evaos-Provider-Grant" not in mint_calls[0][1]
    assert mounted["url"] == "https://remote.mcp.pipedream.net/v3"
    assert {
        name: mounted["headers"][name]
        for name in (
            "Authorization",
            "x-pd-project-id",
            "x-pd-environment",
            "x-pd-external-user-id",
            "x-pd-app-slug",
            "x-pd-account-id",
        )
    } == _lease_payload(
        datetime.now(timezone.utc) + timedelta(minutes=10)
    )["headers"]
    assert isinstance(mounted["auth"], EvaosLeaseHttpAuth)


def test_identity_keyed_managed_config_remains_tolerated():
    task = MCPServerTask("pipedream-google-sheets")
    task._auth_type = "evaos_lease"
    task._validate_evaos_lease_config(
        {
            "auth": "evaos_lease",
            "app_slug": "google_sheets",
            "customer_id": "customer-fixture",
            "agent_runtime": "hermes",
            "agent_id": "agent-fixture",
            "lazy": True,
        }
    )


def test_thin_config_no_longer_fatal_blocks():
    task = MCPServerTask("pipedream-google-sheets")
    task._auth_type = "evaos_lease"
    task._validate_evaos_lease_config(
        {"auth": "evaos_lease", "app_slug": "google_sheets", "lazy": True}
    )


@pytest.mark.asyncio
async def test_lease_mint_failure_warning_is_once_and_profile_scoped(
    tmp_path, caplog
):
    source, _ = _source(tmp_path, profile_key=str(tmp_path))
    task = MCPServerTask("pipedream-google-sheets", str(tmp_path))

    async def unavailable(_url, _headers, _payload):
        return _Response(503, {})

    manager = EvaosLeaseManager(
        source=source,
        transport=unavailable,
        on_mint_failure=task._warn_evaos_lease_failure,
    )
    with caplog.at_level("WARNING", logger="tools.mcp_tool"):
        with pytest.raises(EvaosLeaseError):
            await manager.get_lease()
        with pytest.raises(EvaosLeaseError):
            await manager.get_lease()

    records = [r for r in caplog.records if "lease mint failed" in r.message]
    assert len(records) == 1
    assert "pipedream-google-sheets" in records[0].message
    assert tmp_path.name in records[0].message
    assert "apn_" not in records[0].message


@pytest.mark.parametrize(
    "override",
    [
        {"url": "https://attacker.example/mcp"},
        {"headers": {"Authorization": "static-secret"}},
        {"command": "fake-mcp"},
        {"transport": "sse"},
        {"ssl_verify": False},
        {"app_slug": "Google Sheets"},
    ],
)
def test_managed_lease_config_rejects_connection_and_auth_overrides(override):
    task = MCPServerTask("pipedream-google-sheets")
    task._auth_type = "evaos_lease"
    config = {
        "auth": "evaos_lease",
        "app_slug": "google_sheets",
        **override,
    }

    with pytest.raises(EvaosLeaseError, match="managed MCP"):
        task._validate_evaos_lease_config(config)


def test_schema_cache_fingerprint_includes_managed_app_identity():
    sheets = config_fingerprint(
        {
            "auth": "evaos_lease",
            "app_slug": "google_sheets",
        }
    )
    drive = config_fingerprint(
        {
            "auth": "evaos_lease",
            "app_slug": "google_drive",
        }
    )
    static = config_fingerprint(
        {
            "url": "https://remote.mcp.pipedream.net/v3",
            "app_slug": "google_sheets",
        }
    )

    assert len({sheets, drive, static}) == 3
