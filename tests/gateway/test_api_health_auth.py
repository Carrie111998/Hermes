"""/health must honor API_SERVER_KEY like every other route (#90315).

The simple ``GET /health`` handler skipped ``_check_auth`` entirely, so a
key-configured listener answered 200 to anonymous probes while
``/v1/models`` and ``/v1/chat/completions`` correctly returned 401 — and
while the sibling ``/health/detailed`` already enforced the bearer. An
unauthenticated 200 made the listener look open when chat was gated.

Contract here: with a key configured, all three health routes (``/health``,
``/health/detailed``, ``/v1/health``) require the Bearer; without one,
they stay open so plain orchestrator probes keep working.
"""

from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _bare_app(adapter: APIServerAdapter):
    from aiohttp import web

    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_get("/health/detailed", adapter._handle_health_detailed)
    app.router.add_get("/v1/health", adapter._handle_health)
    return app


@pytest.mark.asyncio
async def test_health_with_key_requires_bearer():
    adapter = _make_adapter("sk-secret")
    app = _bare_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        anon = await cli.get("/health")
        assert anon.status == 401

        wrong = await cli.get(
            "/health", headers={"Authorization": "Bearer sk-wrong"}
        )
        assert wrong.status == 401

        good = await cli.get(
            "/health", headers={"Authorization": "Bearer sk-secret"}
        )
        assert good.status == 200
        data = await good.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_v1_health_with_key_requires_bearer():
    adapter = _make_adapter("sk-secret")
    app = _bare_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        assert (await cli.get("/v1/health")).status == 401
        ok = await cli.get(
            "/v1/health", headers={"Authorization": "Bearer sk-secret"}
        )
        assert ok.status == 200


@pytest.mark.asyncio
async def test_health_without_key_stays_open_for_probes():
    """No key configured → plain orchestrator healthchecks keep working."""
    adapter = _make_adapter("")
    app = _bare_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_no_key_regression_existing_health_tests_unaffected():
    """Parity guard mirroring TestHealthEndpoint's no-key expectations."""
    adapter = _make_adapter("")
    app = _bare_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        for path in ("/health", "/v1/health"):
            resp = await cli.get(path)
            assert resp.status == 200
