"""Route-level exact source filtering for webhook subscriptions."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _adapter(filters_marker=...):
    route = {
        "secret": _INSECURE_NO_AUTH,
        "events": [],
        "script": "synthetic-script",
        "prompt": "safe {source}",
        "skills": [],
        "toolsets": [],
    }
    if filters_marker is not ...:
        route["filters"] = filters_marker
    config = PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"r": route},
    })
    adapter = WebhookAdapter(config)
    adapter._route_processor.run_route_script = MagicMock(return_value=(True, {"source": "agl-b2b-account"}))
    adapter.handle_message = AsyncMock()
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return adapter, app


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], "x", 1, None, True])
async def test_non_object_payload_is_400_before_every_downstream_effect(payload):
    adapter, app = _adapter({"source": "agl-b2b-account"})
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/r", data=json.dumps(payload))
    assert response.status == 400
    adapter._route_processor.run_route_script.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("filters", [{}, {"source": "bad"}, {"source": "agl-b2b-account", "x": 1}])
async def test_malformed_filter_config_fails_closed(filters):
    adapter, app = _adapter(filters)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/r", json={"source": "agl-b2b-account"})
    assert response.status == 403
    adapter._route_processor.run_route_script.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "agl-b2b-quotes", 1, [], {}])
async def test_source_mismatch_is_generic_403_before_side_effects(source):
    adapter, app = _adapter({"source": "agl-b2b-account"})
    payload = {} if source is None else {"source": source}
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/r", json=payload)
        body = await response.text()
    assert response.status == 403
    assert "agl-b2b" not in body
    adapter._route_processor.run_route_script.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_source_and_empty_events_proceed_normally():
    adapter, app = _adapter({"source": "agl-b2b-account"})
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/r", json={"source": "agl-b2b-account"})
    assert response.status == 202
    adapter._route_processor.run_route_script.assert_called_once()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_generic_object_filter_is_still_evaluated():
    adapter, app = _adapter({"field": "source", "equals": "allowed"})
    async with TestClient(TestServer(app)) as client:
        rejected = await client.post("/webhooks/r", json={"source": "blocked"})
        rejected_body = await rejected.json()
        accepted = await client.post("/webhooks/r", json={"source": "allowed"})
    assert rejected.status == 200
    assert rejected_body["reason"] == "filter"
    assert accepted.status == 202
    adapter._route_processor.run_route_script.assert_called_once()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_without_filters_preserves_existing_behavior():
    adapter, app = _adapter()
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/r", json={"source": "anything"})
    assert response.status == 202
    adapter._route_processor.run_route_script.assert_called_once()
    adapter.handle_message.assert_awaited_once()
