"""Fail-closed request and durable-carrier limits for webhook intake."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    WebhookConfigurationError,
    _MAX_BODY_BYTES_LIMIT,
)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Keep each test's durable ledger and key authority independent."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _adapter(*, max_body_bytes=_MAX_BODY_BYTES_LIMIT, routes=None, **extra):
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "max_body_bytes": max_body_bytes,
                "routes": routes or {},
                **extra,
            },
        )
    )


def _app(adapter):
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


@pytest.mark.parametrize(
    "configured",
    [
        True,
        False,
        1.0,
        1.5,
        _MAX_BODY_BYTES_LIMIT + 1,
    ],
)
def test_max_body_bytes_rejects_non_integer_or_unsafe_configuration(configured):
    with pytest.raises(WebhookConfigurationError, match="max_body_bytes"):
        _adapter(max_body_bytes=configured)


@pytest.mark.parametrize(
    "field",
    ["rate_limit", "script_timeout_seconds"],
)
@pytest.mark.parametrize(
    "configured",
    [True, False, None, 0, -1, 1.5, "1.5"],
)
def test_execution_limits_reject_non_positive_or_non_integer_configuration(
    field,
    configured,
):
    with pytest.raises(WebhookConfigurationError, match=field):
        _adapter(**{field: configured})


@pytest.mark.parametrize(
    ("field", "configured"),
    [
        ("port", -1),
        ("port", 65_536),
        ("rate_limit", 10_001),
        ("script_timeout_seconds", 301),
    ],
)
def test_execution_limits_reject_values_above_their_safe_bounds(
    field,
    configured,
):
    with pytest.raises(WebhookConfigurationError, match=field):
        _adapter(**{field: configured})


@pytest.mark.asyncio
async def test_signed_template_expansion_is_rejected_before_effect_and_recovers():
    secret = "carrier-limit-secret"
    adapter = _adapter(
        routes={
            "events": {
                "provider": "generic",
                "signature_mode": "generic_v1",
                "secret": secret,
                "prompt": "{data}{data}",
            }
        }
    )
    adapter.handle_message = AsyncMock()

    def signed(payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return body, {
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature,
        }

    oversized_body, oversized_headers = signed({"data": "x" * (300 * 1024)})
    small_body, small_headers = signed({"data": "ok"})

    async with TestClient(TestServer(_app(adapter))) as client:
        rejected = await client.post(
            "/webhooks/events",
            data=oversized_body,
            headers=oversized_headers,
        )
        rejected_payload = await rejected.json()
        assert adapter._operation_ledger.count() == 0
        healthy = await client.get("/health")
        accepted = await client.post(
            "/webhooks/events",
            data=small_body,
            headers=small_headers,
        )

    assert rejected.status == 413
    assert rejected_payload == {
        "error": "Payload expands beyond durable webhook limits"
    }
    assert healthy.status == 200
    assert accepted.status == 202
    assert adapter._operation_ledger.count() == 1
    adapter.handle_message.assert_awaited_once()
