"""Authority tests for durable webhook authentication-key ownership."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import webhook_intake as webhook_intake_module
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_ledger import (
    WebhookLedgerError,
    WebhookLedgerTransitionError,
)


_LONG_HMAC_KEY = "a" * 65
_LONG_HMAC_DIGEST_WHSEC = (
    "whsec_"
    + base64.b64encode(hashlib.sha256(_LONG_HMAC_KEY.encode()).digest()).decode()
)


def _adapter(routes, **extra) -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": routes, **extra},
        )
    )


async def _assert_invalid_connect(adapter: WebhookAdapter, message: str) -> None:
    assert await adapter.connect() is False
    assert adapter._runner is None
    assert adapter.fatal_error_code == "webhook_configuration_invalid"
    assert adapter.fatal_error_retryable is False
    assert message in (adapter.fatal_error_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routes",
    [
        {
            "first": {"secret": "shared-secret", "provider": "generic"},
            "second": {"secret": "shared-secret", "provider": "github"},
        },
        {
            "raw": {"secret": "secret", "provider": "generic"},
            "decoded": {
                "secret": "whsec_c2VjcmV0",
                "provider": "standard_webhooks",
            },
        },
        {
            "raw": {
                "secret": "whsec_c2VjcmV0",
                "provider": "generic",
            },
            "decoded": {
                "secret": "whsec_c2VjcmV0",
                "provider": "standard_webhooks",
            },
        },
        {
            "plain": {"secret": "shared", "provider": "generic"},
            "zero-padded": {
                "secret": "shared\0",
                "provider": "generic",
            },
        },
        {
            "long": {"secret": _LONG_HMAC_KEY, "provider": "generic"},
            "digest": {
                "secret": _LONG_HMAC_DIGEST_WHSEC,
                "provider": "standard_webhooks",
            },
        },
    ],
)
async def test_connect_rejects_secret_material_shared_across_routes(routes):
    adapter = _adapter(routes)

    await _assert_invalid_connect(adapter, "must not reuse secret material")


@pytest.mark.asyncio
async def test_connect_rejects_global_secret_fallback_for_multiple_routes():
    adapter = _adapter(
        {
            "first": {"provider": "generic"},
            "second": {"provider": "generic"},
        },
        secret="shared-global-secret",
    )

    await _assert_invalid_connect(adapter, "must not reuse secret material")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["svix", "standard_webhooks"])
async def test_connect_rejects_empty_decoded_whsec_key(provider):
    adapter = _adapter({
        "hook": {"secret": "whsec_", "provider": provider},
    })

    await _assert_invalid_connect(adapter, "must decode to a non-empty key")


@pytest.mark.asyncio
async def test_connect_classifies_surrogate_event_as_nonretryable_configuration():
    adapter = _adapter({
        "hook": {
            "secret": "event-secret",
            "provider": "github",
            "events": ["\udcff"],
        },
    })

    await _assert_invalid_connect(adapter, "event is not valid Unicode")


@pytest.mark.asyncio
async def test_invalid_startup_does_not_permanently_consume_key_binding():
    invalid = _adapter({
        "hook": {
            "secret": "correctable-secret",
            "provider": "generic",
            "deliver_only": True,
            "deliver": "log",
        },
    })
    await _assert_invalid_connect(invalid, "deliver_only=true")

    corrected = _adapter({
        "hook": {
            "secret": "correctable-secret",
            "provider": "generic",
            "deliver_only": True,
            "deliver": "telegram",
            "deliver_extra": {"chat_id": "12345"},
        },
    })
    assert await corrected.connect() is True
    await corrected.disconnect()


@pytest.mark.asyncio
async def test_deep_route_policy_is_fatal_nonretryable_configuration():
    nested: object = "value"
    for _ in range(2_000):
        nested = {"nested": nested}
    adapter = _adapter({
        "hook": {
            "secret": "deep-policy-secret",
            "provider": "generic",
            "custom_policy": nested,
        }
    })

    await _assert_invalid_connect(adapter, "execution policy")


@pytest.mark.parametrize(
    ("mode", "id_header", "time_header", "signature_header"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
        ),
    ],
)
def test_verifier_rejects_signature_forged_with_empty_whsec_key(
    mode,
    id_header,
    time_header,
    signature_header,
):
    body = b'{"event":"forged"}'
    message_id = "message-1"
    timestamp = str(int(time.time()))
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    forged = (
        "v1,"
        + base64.b64encode(hmac.new(b"", signed, hashlib.sha256).digest()).decode()
    )
    request = type(
        "Request",
        (),
        {
            "headers": {
                id_header: message_id,
                time_header: timestamp,
                signature_header: forged,
            },
            "match_info": {"route_name": "hook"},
        },
    )()
    adapter = _adapter({
        "hook": {"secret": "whsec_", "provider": mode},
    })

    assert adapter._validate_signature(request, body, "whsec_", mode) is False


def test_persisted_key_cannot_move_to_renamed_route_after_restart():
    first = _adapter({
        "old-route": {"secret": "durable-secret", "provider": "generic"},
    })
    first._bind_route_authentication_authorities(first._routes)
    replacement = _adapter({
        "new-route": {"secret": "durable-secret", "provider": "generic"},
    })

    with pytest.raises(WebhookLedgerTransitionError):
        replacement._bind_route_authentication_authorities(replacement._routes)


def test_key_cannot_move_between_named_single_profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile_a = root / "profiles" / "alpha"
    profile_b = root / "profiles" / "beta"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    route = {
        "hook": {"secret": "cross-profile-secret", "provider": "generic"},
    }

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    first = _adapter(route)
    first._bind_route_authentication_authorities(first._routes)

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    replacement = _adapter(route)
    assert (
        first._authentication_authority_ledger.db_path
        == replacement._authentication_authority_ledger.db_path
        == root / "state.db"
    )
    with pytest.raises(WebhookLedgerTransitionError):
        replacement._bind_route_authentication_authorities(replacement._routes)


def test_policy_elevation_requires_key_rotation_after_restart():
    first = _adapter({
        "hook": {
            "secret": "old-policy-secret",
            "provider": "generic",
            "events": ["notice"],
            "toolsets": [],
        },
    })
    first._bind_route_authentication_authorities(first._routes)
    elevated = _adapter({
        "hook": {
            "secret": "old-policy-secret",
            "provider": "generic",
            "events": ["admin"],
            "toolsets": ["terminal"],
        },
    })

    with pytest.raises(WebhookLedgerTransitionError):
        elevated._bind_route_authentication_authorities(elevated._routes)

    rotated = _adapter({
        "hook": {
            "secret": "rotated-policy-secret",
            "provider": "generic",
            "events": ["admin"],
            "toolsets": ["terminal"],
        },
    })
    rotated._bind_route_authentication_authorities(rotated._routes)


@pytest.mark.asyncio
async def test_handler_bypass_of_connect_rejects_duplicate_key_without_effect():
    secret = "duplicate-handler-secret"
    adapter = _adapter({
        "first": {
            "secret": secret,
            "provider": "generic",
            "signature_mode": "generic_v1",
        },
        "second": {
            "secret": secret,
            "provider": "generic",
            "signature_mode": "generic_v1",
        },
    })
    adapter.handle_message = AsyncMock()
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    body = json.dumps({"event": "notice"}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/first",
            data=body,
            headers={"X-Webhook-Signature": signature},
        )
        health = await client.get("/health")

    assert response.status == 500
    assert health.status == 200
    assert adapter._operation_ledger.count() == 0
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_bound_many_route_listener_checks_only_selected_route(monkeypatch):
    routes = {
        f"route-{index}": {
            "secret": f"secret-{index}",
            "provider": "generic",
            "signature_mode": "generic_v1",
        }
        for index in range(200)
    }
    adapter = _adapter(routes)
    adapter._bind_route_authentication_authorities(adapter._routes)

    def fail_full_snapshot(_routes):
        raise AssertionError("request path rebuilt the complete route set")

    monkeypatch.setattr(
        adapter,
        "_route_authentication_authority_snapshot",
        fail_full_snapshot,
    )
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/route-0",
            json={"event": "notice"},
            headers={"X-Webhook-Signature": "invalid"},
        )

    assert response.status == 401


def test_transient_binding_failure_withdraws_old_and_retries_candidate(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    subscriptions = tmp_path / "webhook_subscriptions.json"
    subscriptions.write_text(
        json.dumps({
            "hook": {"secret": "old-secret", "provider": "generic"},
        })
    )
    adapter = _adapter({})
    adapter._reload_dynamic_routes()
    assert adapter._routes["hook"]["secret"] == "old-secret"

    subscriptions.write_text(
        json.dumps({
            "hook": {"secret": "new-secret", "provider": "generic"},
        })
    )
    real_bind = adapter._authentication_authority_ledger.bind_authentication_keys
    attempts = 0

    def transient_once(bindings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WebhookLedgerError("temporary store failure")
        return real_bind(bindings)

    clock = [100.0]
    monkeypatch.setattr(
        adapter._authentication_authority_ledger,
        "bind_authentication_keys",
        transient_once,
    )
    monkeypatch.setattr(
        webhook_intake_module.time,
        "monotonic",
        lambda: clock[0],
    )

    adapter._reload_dynamic_routes()
    assert "hook" not in adapter._routes
    adapter._reload_dynamic_routes()
    assert attempts == 1
    assert "hook" not in adapter._routes

    clock[0] = 101.1
    adapter._reload_dynamic_routes()

    assert attempts == 2
    assert adapter._routes["hook"]["secret"] == "new-secret"
