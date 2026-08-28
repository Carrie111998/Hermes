"""Public liveness/readiness contract for the sharded webhook adapter."""

from __future__ import annotations

import errno
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _adapter() -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "events": {
                        "provider": "generic",
                        "secret": "readiness-secret-must-not-leak",
                    }
                },
            },
        )
    )


@pytest.mark.asyncio
async def test_health_is_liveness_only_when_intake_is_fenced():
    adapter = _adapter()
    adapter._accepting_webhooks = False
    adapter._operation_ledger.has_global_admission_capacity = MagicMock(
        side_effect=AssertionError("liveness must not probe durable storage")
    )
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/health")
        assert response.status == 200
        assert await response.json() == {
            "status": "ok",
            "platform": "webhook",
        }


@pytest.mark.asyncio
async def test_ready_reports_fenced_intake_without_secret_or_route_policy():
    adapter = _adapter()
    adapter._accepting_webhooks = False
    app = web.Application()
    app.router.add_get("/ready", adapter._handle_ready)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/ready")
        body = await response.text()
    assert response.status == 503
    assert "not_ready" in body
    assert "readiness-secret-must-not-leak" not in body
    assert "signature_mode" not in body


@pytest.mark.asyncio
async def test_ready_reports_only_bounded_operational_fields():
    adapter = _adapter()
    adapter._accepting_webhooks = True
    adapter._bind_route_authentication_authorities(adapter._routes)
    app = web.Application()
    app.router.add_get("/ready", adapter._handle_ready)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/ready")
        payload = await response.json()
    assert response.status == 200
    assert payload == {
        "status": "ready",
        "platform": "webhook",
        "accepting_webhooks": True,
        "host": "127.0.0.1",
        "port": 0,
        "routes": 1,
    }


@pytest.mark.asyncio
async def test_port_conflict_is_fatal_and_nonretryable():
    adapter = _adapter()
    site = MagicMock()
    site.start = AsyncMock(side_effect=OSError(errno.EADDRINUSE, "in use"))
    with patch("gateway.platforms.webhook.web.TCPSite", return_value=site):
        assert await adapter.connect() is False
    assert adapter.fatal_error_code == "webhook_port_in_use"
    assert adapter.fatal_error_retryable is False


@pytest.mark.asyncio
async def test_other_bind_failure_is_named_and_retryable():
    adapter = _adapter()
    site = MagicMock()
    site.start = AsyncMock(side_effect=OSError(errno.EACCES, "denied"))
    with patch("gateway.platforms.webhook.web.TCPSite", return_value=site):
        assert await adapter.connect() is False
    assert adapter.fatal_error_code == "webhook_bind_failed"
    assert adapter.fatal_error_retryable is True
