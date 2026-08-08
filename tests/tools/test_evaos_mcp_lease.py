"""Managed evaOS MCP lease authentication and refresh behavior."""

from __future__ import annotations

import asyncio
import json
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
    grants = tmp_path / "grants.json"
    _write_secret(broker, "broker-secret-under-test\n")
    _write_secret(
        grants,
        json.dumps(
            {
                "google_sheets": "grant_google_sheets_123456",
                "google_drive": "grant_google_drive_12345678",
            }
        ),
    )
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": str(broker),
        "PIPEDREAM_PROVIDER_GRANT_FILE": str(grants),
    }
    source = EvaosLeaseSource(
        profile_key=profile_key,
        app_slug=app_slug,
        secret_reader=values.get,
        profile_resolver=lambda: profile_key,
        root_uid=os.getuid(),
    )
    return source, broker, grants


def _lease_payload(expires_at: datetime, token="lease-token-1"):
    return {
        "mcp_url": "https://remote.mcp.pipedream.net/v3",
        "headers": {
            "Authorization": f"Bearer {token}",
            "x-pd-project-id": "proj",
            "x-pd-environment": "production",
            "x-pd-external-user-id": "server-derived",
            "x-pd-app-slug": "google_sheets",
            "x-pd-account-id": "server-resolved",
        },
        "expires_at": expires_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_lease_request_body_contains_only_action_and_app_slug(tmp_path):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _, _ = _source(tmp_path)
    calls = []

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
    assert calls[0][1]["X-Evaos-Provider-Grant"] == (
        "grant_google_sheets_123456"
    )
    assert not {
        "customer_id",
        "account_id",
        "profile",
        "profile_id",
        "external_user_id",
        "agent_id",
    } & calls[0][2].keys()


@pytest.mark.asyncio
async def test_expiry_refreshes_before_skew(tmp_path):
    clock = [datetime(2026, 8, 8, tzinfo=timezone.utc)]
    source, _, _ = _source(tmp_path)
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
    source, _, _ = _source(tmp_path)
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
    source, _, grants = _source(tmp_path)
    calls = 0
    grants_seen = []

    async def transport(url, headers, payload):
        nonlocal calls
        calls += 1
        grants_seen.append(headers["X-Evaos-Provider-Grant"])
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
    _write_secret(
        grants,
        json.dumps({"google_sheets": "grant_rotated_1234567890"}),
    )
    second = await flow.asend(httpx.Response(401, request=first))
    assert second.headers["Authorization"] == "Bearer lease-token-2"
    assert calls == 2
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(401, request=second))
    assert calls == 2
    assert grants_seen == [
        "grant_google_sheets_123456",
        "grant_rotated_1234567890",
    ]


def test_source_rejects_cross_profile_and_unsafe_files(tmp_path):
    source, _, grants = _source(tmp_path)
    source._profile_resolver = lambda: "profile-b"
    with pytest.raises(EvaosLeaseError, match="profile authority"):
        source.read()

    source._profile_resolver = lambda: "profile-a"
    grants.chmod(0o644)
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
    grants = credentials / "pipedream_provider_grants"
    _write_secret(broker, "broker-secret-under-test\n")
    _write_secret(
        grants,
        json.dumps({"google_sheets": "grant_google_sheets_123456"}),
    )
    broker.chmod(mode)
    grants.chmod(mode)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": str(broker),
        "PIPEDREAM_PROVIDER_GRANT_FILE": str(grants),
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
    grants = credentials / "pipedream_provider_grants"
    _write_secret(broker, "broker-secret-under-test\n")
    _write_secret(
        grants,
        json.dumps({"google_sheets": "grant_google_sheets_123456"}),
    )
    broker.chmod(0o400)
    grants.chmod(0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    values = {
        "EVAOS_DESKTOP_RUNTIME_SESSION_URL": (
            "https://example.supabase.co/functions/v1/desktop-runtime-session"
        ),
        "PIPEDREAM_AGENT_BROKER_SECRET_FILE": "%d/pipedream_broker",
        "PIPEDREAM_PROVIDER_GRANT_FILE": (
            "%d/pipedream_provider_grants"
        ),
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
    source, broker, grants = _source(tmp_path)
    broker.chmod(0o400)
    grants.chmod(0o400)

    with pytest.raises(EvaosLeaseError, match="secure managed credential"):
        source.read()


def test_source_rejects_unsafe_systemd_credential_pointer(tmp_path, monkeypatch):
    source, _, _ = _source(tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    original_reader = source._secret_reader
    source._secret_reader = lambda name: (
        "%d/../provider-grants"
        if name == "PIPEDREAM_PROVIDER_GRANT_FILE"
        else original_reader(name)
    )

    with pytest.raises(EvaosLeaseError, match="systemd credential pointer"):
        source.read()


def test_source_rejects_symlinked_secret_file(tmp_path):
    source, broker, _ = _source(tmp_path)
    target = tmp_path / "broker-target"
    broker.rename(target)
    broker.symlink_to(target)

    with pytest.raises(EvaosLeaseError, match="broker secret file"):
        source.read()


def test_runtime_endpoint_and_broker_path_are_global_but_grant_path_is_scoped(
    monkeypatch,
):
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
        "PIPEDREAM_PROVIDER_GRANT_FILE",
        "/wrong-cross-profile-grants",
    )
    monkeypatch.setenv(
        "CREDENTIALS_DIRECTORY",
        "/run/credentials/evaos-shared-gateway.service",
    )
    token = secret_scope.set_secret_scope(
        {
            "PIPEDREAM_PROVIDER_GRANT_FILE": "/profile/grants",
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
        assert secret_scope.get_secret(
            "PIPEDREAM_PROVIDER_GRANT_FILE"
        ) == "/profile/grants"
        assert secret_scope.get_secret("CREDENTIALS_DIRECTORY") == (
            "/run/credentials/evaos-shared-gateway.service"
        )
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


def test_source_rejects_non_supabase_lease_endpoint(tmp_path):
    source, _, _ = _source(tmp_path)
    original_reader = source._secret_reader
    source._secret_reader = lambda name: (
        "https://attacker.example/functions/v1/desktop-runtime-session"
        if name == "EVAOS_DESKTOP_RUNTIME_SESSION_URL"
        else original_reader(name)
    )

    with pytest.raises(EvaosLeaseError, match="lease endpoint"):
        source.read()


@pytest.mark.parametrize(
    "raw",
    [
        '{"google_sheets":"grant_1234567890123456","google_sheets":"other_1234567890123456"}',
        '{"google_sheets":{"grant":"nested"}}',
        '{"Google Sheets":"grant_1234567890123456"}',
        '{"google_sheets":"short"}',
    ],
)
def test_grant_map_rejects_duplicates_nested_values_and_unsafe_entries(
    tmp_path,
    raw,
):
    source, _, grants = _source(tmp_path)
    _write_secret(grants, raw)
    with pytest.raises(EvaosLeaseError, match="provider grant"):
        source.read()


@pytest.mark.asyncio
async def test_errors_and_repr_never_expose_tokens(tmp_path, caplog):
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    source, _, _ = _source(tmp_path)
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
    assert "grant_google_sheets_123456" not in rendered


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
    source, _, _ = _source(tmp_path)
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
        {"auth": "evaos_lease", "app_slug": "google_sheets"}
    )
    drive = config_fingerprint(
        {"auth": "evaos_lease", "app_slug": "google_drive"}
    )
    static = config_fingerprint(
        {
            "url": "https://remote.mcp.pipedream.net/v3",
            "app_slug": "google_sheets",
        }
    )

    assert len({sheets, drive, static}) == 3
