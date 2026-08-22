"""Behavior contracts for authenticated webhook profile handoffs."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import (
    SessionContext,
    SessionEntry,
    SessionSource,
    build_session_context_prompt,
)


def _signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _v2_headers(body: bytes, *, request_id: str, timestamp: str) -> dict[str, str]:
    signed = timestamp.encode() + b"." + body
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": hmac.new(
            b"relay-secret", signed, hashlib.sha256
        ).hexdigest(),
        "X-Request-ID": request_id,
    }


def _svix_headers(body: bytes, *, message_id: str, timestamp: str) -> dict[str, str]:
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(
        hmac.new(b"relay-secret", signed, hashlib.sha256).digest()
    ).decode()
    return {
        "Content-Type": "application/json",
        "svix-id": message_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature}",
    }


def _adapter(route: dict, *, multiplex: bool = True) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": {"relay": route}},
        )
    )

    class Runner:
        config = GatewayConfig(
            multiplex_profiles=multiplex,
            multiplex_profile_allowlist=["dispatcher", "market-analysis", "server-development"],
        )

        @staticmethod
        def _profile_name_for_source(source):
            return None

    adapter.gateway_runner = Runner()
    return adapter


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/p/{profile}/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _trusted_route(**overrides) -> dict:
    route = {
        "secret": "relay-secret",
        "profile": "dispatcher",
        "prompt": "Task: {task}",
        "allowed_target_profiles": ["market-analysis", "server-development"],
        "allowed_target_toolsets": {
            "market-analysis": ["web", "terminal"],
            "server-development": ["web", "terminal", "file"],
        },
        "max_handoff_depth": 1,
        "max_handoff_concurrency": 2,
        "deliver": "discord",
        "deliver_extra": {"chat_id": "market-room"},
    }
    route.update(overrides)
    return route


@pytest.fixture
def served_profiles(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [
            (name, f"/profiles/{name}")
            for name in ("default", "dispatcher", "market-analysis", "server-development")
        ],
    )


@pytest.fixture(autouse=True)
def isolated_handoff_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


@pytest.mark.asyncio
async def test_authenticated_selector_hands_off_to_allowlisted_profile(served_profiles):
    adapter = _adapter(_trusted_route())
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        events.append(event)

    adapter.handle_message = capture
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "handoff-allowed",
        },
        "task": "Summarize the market with the local CLI",
    }
    body = json.dumps(payload).encode()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
                "X-GitHub-Delivery": "handoff-allowed",
            },
        )
        assert response.status == 202
        accepted = await response.json()
        assert accepted["target_profile"] == "market-analysis"

    await asyncio.sleep(0.05)
    assert len(events) == 1
    source = events[0].source
    assert source.platform.value == "webhook"
    assert source.profile == "market-analysis"
    assert source.transport_profile == "dispatcher"
    assert source.trusted_handoff_depth == 1
    assert source.transport_deliver == "discord"
    assert source.transport_deliver_extra == {"chat_id": "market-room"}
    assert "_hermes" not in events[0].raw_message
    assert sorted(adapter.toolsets_for_source(source)) == ["terminal", "web"]
    assert source.provenance == {
        "ingress_platform": "webhook",
        "ingress_route": "relay",
        "source_profile": "dispatcher",
        "target_profile": "market-analysis",
        "effective_toolsets": ["terminal", "web"],
        "delivery_platform": "discord",
        "delivery_chat_id": "market-room",
        "handoff_depth": 1,
    }
    assert SessionSource.from_dict(source.to_dict()).provenance == source.provenance


@pytest.mark.asyncio
async def test_generic_v2_handoff_replay_ignores_unsigned_request_id_after_restart(
    served_profiles,
):
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "signed-v2-delivery",
        },
        "task": "run once",
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        events.append(event)

    first_adapter = _adapter(_trusted_route())
    first_adapter.handle_message = capture
    async with TestClient(TestServer(_app(first_adapter))) as client:
        first = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers=_v2_headers(body, request_id="unsigned-A", timestamp=timestamp),
        )
        assert first.status == 202
        assert (await first.json())["delivery_id"] == "signed-v2-delivery"

    await asyncio.sleep(0.05)
    restarted_adapter = _adapter(_trusted_route())
    restarted_adapter.handle_message = capture
    async with TestClient(TestServer(_app(restarted_adapter))) as client:
        replay = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers=_v2_headers(body, request_id="unsigned-B", timestamp=timestamp),
        )
        assert replay.status == 200
        assert await replay.json() == {
            "status": "duplicate",
            "delivery_id": "signed-v2-delivery",
        }

    await asyncio.sleep(0.05)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_svix_signed_handoff_id_accepts_once_and_deduplicates_retry(
    served_profiles,
):
    adapter = _adapter(_trusted_route())
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        events.append(event)

    adapter.handle_message = capture
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "signed-svix-delivery",
        },
        "task": "run once",
    }
    body = json.dumps(payload).encode()
    headers = _svix_headers(
        body,
        message_id="msg_provider_signed",
        timestamp=str(int(time.time())),
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        first = await client.post(
            "/p/dispatcher/webhooks/relay", data=body, headers=headers
        )
        retry = await client.post(
            "/p/dispatcher/webhooks/relay", data=body, headers=headers
        )
        assert first.status == 202
        assert retry.status == 200
        assert (await retry.json())["status"] == "duplicate"

    await asyncio.sleep(0.05)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_handoff_replay_store_failure_blocks_dispatch(
    served_profiles, monkeypatch
):
    adapter = _adapter(_trusted_route())
    adapter.handle_message = pytest.fail
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "store-failure",
        },
        "task": "must not run",
    }
    body = json.dumps(payload).encode()

    def fail_claim(**_kwargs):
        raise OSError("state database unavailable")

    monkeypatch.setattr(
        "gateway.webhook_replay.claim_handoff_delivery", fail_claim
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == 503


@pytest.mark.asyncio
async def test_atomic_handoff_claim_has_exactly_one_winner():
    from gateway.webhook_replay import claim_handoff_delivery

    claims = await asyncio.gather(
        *(
            asyncio.to_thread(
                claim_handoff_delivery,
                route_name="relay",
                source_profile="dispatcher",
                delivery_id="concurrent-signed-id",
            )
            for _ in range(8)
        )
    )
    assert claims.count(True) == 1
    assert claims.count(False) == 7


@pytest.mark.asyncio
async def test_persisted_handoff_resolves_ingress_adapter_and_revalidates_bounds(
    served_profiles,
):
    adapter = _adapter(_trusted_route())
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:relay:restart",
        profile="market-analysis",
        transport_profile="dispatcher",
        trusted_handoff_depth=1,
        transport_deliver="discord",
        transport_deliver_extra={"chat_id": "market-room"},
        provenance={"source_profile": "descriptive-only-and-not-trusted"},
    )
    now = datetime.now()
    restored = SessionEntry.from_dict(
        SessionEntry(
            session_key="market-analysis:webhook:restart",
            session_id="restart-session",
            created_at=now,
            updated_at=now,
            origin=source,
            resume_pending=True,
        ).to_dict()
    ).origin
    assert restored is not None
    assert getattr(restored, "_transport_adapter_ref", None) is None

    class Runner(GatewayAuthorizationMixin):
        adapters = {}
        _profile_adapters = {"dispatcher": {Platform.WEBHOOK: adapter}}
        config = GatewayConfig(
            multiplex_profiles=True,
            multiplex_profile_allowlist=[
                "dispatcher",
                "market-analysis",
                "server-development",
            ],
        )

        @staticmethod
        def _active_profile_name():
            return "default"

    runner = Runner()
    target = AsyncMock()
    target.send.return_value = SendResult(success=True)
    runner.adapters = {Platform.DISCORD: target}
    adapter.gateway_runner = runner
    assert adapter._handoff_config_error(adapter._routes["relay"]) is None
    assert "market-analysis" in adapter._served_profile_names()
    assert restored.transport_deliver == "discord"
    assert restored.transport_deliver_extra == {"chat_id": "market-room"}
    assert adapter.validate_restored_source(restored) is True
    assert runner._adapter_for_source(restored) is adapter
    assert adapter.toolsets_for_source(restored) == ["web", "terminal"]
    assert restored.profile == "market-analysis"
    result = await adapter.send(restored.chat_id, "finished after restart")
    assert result.success is True
    target.send.assert_awaited_once_with(
        "market-room", "finished after restart", metadata=None
    )

    adapter._routes["relay"]["deliver"] = "slack"
    assert runner._adapter_for_source(restored) is None
    adapter._routes["relay"]["deliver"] = "discord"

    adapter._routes["relay"]["allowed_target_profiles"] = ["server-development"]
    assert runner._adapter_for_source(restored) is None
    assert adapter.toolsets_for_source(restored) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "status"),
    [
        ({"target_profile": "finance-admin", "handoff_depth": 1}, 403),
        ({"target_profile": "market-analysis", "handoff_depth": 2}, 403),
        (
            {
                "target_profile": "market-analysis",
                "handoff_depth": 1,
                "toolsets": ["terminal", "file"],
            },
            403,
        ),
    ],
)
async def test_denied_target_depth_or_toolset_expansion_fails_closed(
    served_profiles, selector, status
):
    adapter = _adapter(_trusted_route())
    adapter.handle_message = pytest.fail
    payload = {"_hermes": selector, "task": "do work"}
    body = json.dumps(payload).encode()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == status
        assert "error" in await response.json()


@pytest.mark.asyncio
async def test_free_form_target_field_cannot_grant_profile(served_profiles):
    adapter = _adapter(_trusted_route())
    adapter.handle_message = pytest.fail
    payload = {"target_profile": "market-analysis", "task": "do work"}
    body = json.dumps(payload).encode()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == 403


@pytest.mark.asyncio
async def test_unconfigured_static_route_keeps_safe_profile_and_toolset_defaults(served_profiles):
    adapter = _adapter(
        {
            "secret": "relay-secret",
            "profile": "dispatcher",
            "prompt": "Task: {task}",
            "deliver": "discord",
        }
    )
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        events.append(event)

    adapter.handle_message = capture
    body = json.dumps({"task": "ordinary webhook"}).encode()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == 202

    await asyncio.sleep(0.05)
    assert events[0].source.profile == "dispatcher"
    assert events[0].source.provenance is None
    assert adapter.toolsets_for_source(events[0].source) is None


@pytest.mark.asyncio
async def test_ordinary_webhook_keeps_distinct_unsigned_delivery_ids(served_profiles):
    adapter = _adapter(
        {
            "secret": "relay-secret",
            "profile": "dispatcher",
            "prompt": "Task: {task}",
            "deliver": "discord",
        }
    )
    events: list[MessageEvent] = []

    async def capture(event: MessageEvent):
        events.append(event)

    adapter.handle_message = capture
    body = json.dumps({"task": "ordinary repeated body"}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _signature(body, "relay-secret"),
    }
    async with TestClient(TestServer(_app(adapter))) as client:
        first = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={**headers, "X-Request-ID": "ordinary-A"},
        )
        second = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={**headers, "X-Request-ID": "ordinary-B"},
        )
        assert first.status == 202
        assert second.status == 202

    await asyncio.sleep(0.05)
    assert [event.message_id for event in events] == ["ordinary-A", "ordinary-B"]


@pytest.mark.asyncio
async def test_handoff_requires_signed_delivery_id(served_profiles):
    adapter = _adapter(_trusted_route())
    adapter.handle_message = pytest.fail
    payload = {
        "_hermes": {"target_profile": "market-analysis", "handoff_depth": 1},
        "task": "do work",
    }
    body = json.dumps(payload).encode()
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == 403
        assert "delivery_id" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_gitlab_token_cannot_authorize_trusted_handoff(served_profiles):
    adapter = _adapter(_trusted_route())
    adapter.handle_message = pytest.fail
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "gitlab-token-only",
        },
        "task": "do work",
    }
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            json=payload,
            headers={"X-Gitlab-Token": "relay-secret"},
        )
        assert response.status == 403
        assert "body-binding signature" in (await response.json())["error"]


@pytest.mark.asyncio
async def test_handoff_requires_authenticated_static_route(served_profiles):
    adapter = _adapter(_trusted_route(secret=_INSECURE_NO_AUTH))
    adapter.handle_message = pytest.fail
    payload = {
        "_hermes": {"target_profile": "market-analysis", "handoff_depth": 1},
        "task": "do work",
    }

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            json=payload,
        )
        assert response.status == 403


@pytest.mark.asyncio
async def test_handoff_concurrency_limit_rejects_without_starting_another_run(served_profiles):
    adapter = _adapter(_trusted_route(max_handoff_concurrency=1))
    adapter._active_handoffs["relay"].add("webhook:relay:already-running")
    adapter.handle_message = pytest.fail
    payload = {
        "_hermes": {
            "target_profile": "market-analysis",
            "handoff_depth": 1,
            "delivery_id": "concurrency-check",
        },
        "task": "do work",
    }
    body = json.dumps(payload).encode()

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/p/dispatcher/webhooks/relay",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _signature(body, "relay-secret"),
            },
        )
        assert response.status == 429


@pytest.mark.asyncio
async def test_handoff_completion_releases_concurrency_slot():
    adapter = _adapter(_trusted_route())
    chat_id = "webhook:relay:finished"
    adapter._active_handoffs["relay"].add(chat_id)
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=chat_id,
        profile="market-analysis",
        provenance={
            "ingress_route": "relay",
            "target_profile": "market-analysis",
        },
    )
    event = MessageEvent(text="task", source=source)

    await adapter.on_processing_complete(event, outcome={"status": "complete"})

    assert "relay" not in adapter._active_handoffs


def test_handoff_config_rejects_unbounded_or_invalid_toolsets():
    missing_bounds = _trusted_route()
    missing_bounds.pop("allowed_target_toolsets")
    assert "allowed_target_toolsets" in WebhookAdapter._handoff_config_error(
        missing_bounds
    )

    invalid_toolset = _trusted_route(
        allowed_target_toolsets={
            "market-analysis": ["web", "not-a-toolset"],
            "server-development": ["web", "terminal"],
        }
    )
    assert "unknown or webhook-restricted" in WebhookAdapter._handoff_config_error(
        invalid_toolset
    )


def test_handoff_provenance_is_visible_in_session_diagnostics():
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:relay:delivery",
        chat_type="webhook",
        profile="market-analysis",
        provenance={
            "ingress_platform": "webhook",
            "ingress_route": "relay",
            "source_profile": "dispatcher",
            "target_profile": "market-analysis",
            "effective_toolsets": ["terminal", "web"],
            "delivery_platform": "discord",
            "delivery_chat_id": "market-room",
            "handoff_depth": 1,
        },
    )
    prompt = build_session_context_prompt(
        SessionContext(source=source, connected_platforms=[], home_channels={})
    )
    assert "Ingress route: relay" in prompt
    assert "Source profile: dispatcher" in prompt
    assert "Target profile: market-analysis" in prompt
    assert "Effective toolsets: terminal, web" in prompt
    assert "Delivery destination: discord/market-room" in prompt
