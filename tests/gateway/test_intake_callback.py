"""Durable, original-session delivery tests for Hermes intake callbacks."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.session import SessionSource
from hermes_cli.intake_reply_tokens import (
    DELIVERY_LEASE_SECONDS,
    TTL_SECONDS,
    begin_delivery,
    bound_action_args,
    claim_action_tool,
    finish_delivery,
    mint,
    register_actions,
    select_action,
)


SECRET = "callback-secret"


def _payload(token: str, *, delivery_id: str = "delivery-1") -> dict:
    return {
        "event": "link_analyzed",
        "reply_token": token,
        "delivery_id": delivery_id,
        "item_id": 42,
        "analysis_status": "succeeded",
        "analysis_attempt": 1,
        "summary": "Link analysis complete item 42.",
        "numbered_replies": [
            {
                "number": 1,
                "action": "approve_item",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:approve_item",
            },
            {
                "number": 2,
                "action": "save_note_and_approve",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:save_note_and_approve",
            },
            {
                "number": 3,
                "action": "leave_pending",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:leave_pending",
            },
        ],
    }


def _sign(body: bytes, timestamp: str) -> str:
    return hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def _adapter() -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "intake": {
                        "secret": SECRET,
                        "events": ["link_analyzed"],
                        "intake_callback": True,
                    }
                },
            },
        )
    )


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _wire_original_session(adapter: WebhookAdapter, session_id: str) -> AsyncMock:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="original-chat",
        chat_type="dm",
        user_id="operator",
        thread_id="topic-7",
    )
    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    store = MagicMock()
    store.lookup_by_session_id.return_value = SimpleNamespace(origin=source)
    runner = MagicMock()
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: target}
    runner.config.get_home_channel.return_value = None
    adapter.gateway_runner = runner
    return target


async def _post(client: TestClient, payload: dict) -> web.Response:
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    return await client.post(
        "/webhooks/intake",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": _sign(body, timestamp),
        },
    )


def test_reply_token_delivery_survives_restart_and_rejects_ttl(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    token = mint("session-1", now=100)
    origin = {"platform": "telegram", "chat_id": "chat-1", "thread_id": "topic"}
    assert begin_delivery(token, "d-1", origin, now=101).status == "claimed"
    assert finish_delivery(token, "d-1", now=102)
    # A fresh process reads the committed state.db receipt rather than sending
    # the original chat a second time.
    assert begin_delivery(token, "d-1", origin, now=103).status == "delivered"

    expired = mint("session-2", now=100)
    assert begin_delivery(expired, "d-2", origin, now=100 + TTL_SECONDS).status == "expired"

    stuck = mint("session-3", now=100)
    assert begin_delivery(stuck, "d-3", origin, now=101).status == "claimed"
    assert begin_delivery(stuck, "d-3", origin, now=101 + DELIVERY_LEASE_SECONDS).status == "uncertain"
    assert begin_delivery(stuck, "d-3", origin, now=102 + DELIVERY_LEASE_SECONDS).status == "uncertain"


def test_action_ledger_is_attempt_bound_idempotent_and_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    token = mint("session-1", now=100)
    assert begin_delivery(token, "d-1", {"platform": "telegram", "chat_id": "c"}, now=101).status == "claimed"
    assert finish_delivery(token, "d-1", now=102)
    payload = _payload(token)
    assert register_actions(token=token, delivery_id="d-1", session_id="session-1", payload=payload, now=103)

    selected = select_action("session-1", "1", now=104)
    assert selected and selected["action"] == "approve_item"
    duplicate = select_action("session-1", "1", now=105)
    assert duplicate == {"duplicate": True, "choice": 1, "delivery_id": "d-1"}
    bound = bound_action_args("session-1", "mcp_mis_approve_item", {})
    assert bound == {
        "item_id": 42,
        "analysis_attempt": 1,
        "idempotency_key": "link:42:attempt:1:approve_item",
    }
    assert not claim_action_tool(
        "session-1", "mcp_mis_approve_item", {"item_id": 42, "analysis_attempt": 2,
                                                   "idempotency_key": "link:42:attempt:2:approve_item"}
    )
    assert claim_action_tool("session-1", "mcp_mis_approve_item", bound)
    assert not claim_action_tool("session-1", "mcp_mis_approve_item", bound)
    assert not claim_action_tool("session-1", "mcp_mis_process_item", bound)

    prohibited = _payload(token, delivery_id="d-2")
    prohibited["numbered_replies"][0]["action"] = "clone_repository"
    assert not register_actions(token=token, delivery_id="d-2", session_id="session-1", payload=prohibited)

    collision = _payload("different-token", delivery_id="d-1")
    assert not register_actions(
        token="different-token", delivery_id="d-1", session_id="session-2", payload=collision
    )


def test_stale_callback_attempt_cannot_claim_newer_attempt(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    token = mint("session-1", now=100)
    origin = {"platform": "telegram", "chat_id": "c"}
    assert begin_delivery(token, "d-1", origin, now=101).status == "claimed"
    assert finish_delivery(token, "d-1", now=102)
    first = _payload(token, delivery_id="d-1")
    assert register_actions(token=token, delivery_id="d-1", session_id="session-1", payload=first, now=103)
    # A separate later callback in the same session makes an unqualified
    # numeric reply ambiguous; Hermes fails closed instead of acting on either.
    token2 = mint("session-1", now=104)
    assert begin_delivery(token2, "d-2", origin, now=105).status == "claimed"
    assert finish_delivery(token2, "d-2", now=106)
    later = _payload(token2, delivery_id="d-2")
    later["analysis_attempt"] = 2
    for option in later["numbered_replies"]:
        option["attempt"] = 2
        option["idempotency_key"] = f"link:42:attempt:2:{option['action']}"
    assert register_actions(token=token2, delivery_id="d-2", session_id="session-1", payload=later, now=107)
    assert select_action("session-1", "1", now=108) is None


@pytest.mark.asyncio
async def test_callback_delivers_once_and_returns_body_bound_signed_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    token = mint("session-1")
    adapter = _adapter()
    target = _wire_original_session(adapter, "session-1")
    async with TestClient(TestServer(_app(adapter))) as client:
        first = await _post(client, _payload(token))
        assert first.status == 200
        receipt_body = await first.read()
        receipt = json.loads(receipt_body)
        assert receipt == {
            "status": "delivered",
            "delivery_id": "delivery-1",
            "original_session_delivered": True,
            "action_ledger_ready": True,
        }
        receipt_timestamp = first.headers["X-Hermes-Receipt-Timestamp"]
        assert hmac.compare_digest(
            first.headers["X-Hermes-Receipt-Signature-V2"],
            _sign(receipt_body, receipt_timestamp),
        )
        repeated = await _post(client, _payload(token))
        assert repeated.status == 200
    target.send.assert_awaited_once()
    assert target.send.await_args.args[0] == "original-chat"
    assert target.send.await_args.kwargs["metadata"] == {"thread_id": "topic-7"}


@pytest.mark.asyncio
async def test_callback_rejects_wrong_token_without_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    adapter = _adapter()
    target = _wire_original_session(adapter, "session-1")
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await _post(client, _payload("not-a-real-token"))
        assert response.status == 404
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_replay_attack_rejected(monkeypatch, tmp_path):
    """Replaying a delivery ID with mismatched payload or after delivery fails closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    token = mint("session-replay")
    adapter = _adapter()
    target = _wire_original_session(adapter, "session-replay")
    async with TestClient(TestServer(_app(adapter))) as client:
        # First valid delivery
        p1 = _payload(token, delivery_id="delivery-replay-1")
        r1 = await _post(client, p1)
        assert r1.status == 200

        # Immediate replay with different payload content
        p2 = _payload(token, delivery_id="delivery-replay-1")
        p2["summary"] = "Tampered summary replay"
        r2 = await _post(client, p2)
        # Idempotent receipt returned without re-dispatching
        assert r2.status == 200
    # Target only received the message exactly once
    assert target.send.await_count == 1


@pytest.mark.asyncio
async def test_callback_expired_token_rejected(monkeypatch, tmp_path):
    """Callback after TTL expiration is refused and fails closed with expired status."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    past_time = int(time.time()) - (25 * 3600)  # 25 hours ago
    token = mint("session-expired", now=past_time)
    adapter = _adapter()
    target = _wire_original_session(adapter, "session-expired")
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await _post(client, _payload(token, delivery_id="delivery-expired-1"))
        assert response.status == 409
        body = await response.read()
        parsed = json.loads(body)
        assert parsed["error"] == "Rejected callback capability"
    target.send.assert_not_awaited()


def test_authz_mixin_intake_action_authorized_positive(tmp_path, monkeypatch):
    """Positive test: an intake action tool call registered in the ledger is authorized."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from gateway.authz_mixin import GatewayAuthorizationMixin as AuthzMixin
    from gateway.session import SessionSource
    from hermes_cli.intake_reply_tokens import mint, register_actions, select_action, begin_delivery, finish_delivery

    session_id = "session-authz-pos"
    token = mint(session_id)
    begin_delivery(token=token, delivery_id="deliv-pos", origin={"platform": "cli", "chat_id": "c1"})
    finish_delivery(token, "deliv-pos")
    payload = {
        "item_id": 42,
        "analysis_attempt": 1,
        "analysis_status": "succeeded",
        "numbered_replies": [
            {
                "number": 1,
                "action": "approve_item",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:approve_item",
            },
            {
                "number": 2,
                "action": "save_note_and_approve",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:save_note_and_approve",
            },
            {
                "number": 3,
                "action": "leave_pending",
                "attempt": 1,
                "idempotency_key": "link:42:attempt:1:leave_pending",
            },
        ],
    }
    assert register_actions(token=token, delivery_id="deliv-pos", session_id=session_id, payload=payload) is True
    assert select_action(session_id, "1") is not None

    mixin = AuthzMixin()
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=session_id,
        user_id="webhook-user",
    )
    source.action_tool = "approve_item"
    source.action_args = {
        "item_id": 42,
        "analysis_attempt": 1,
        "idempotency_key": "link:42:attempt:1:approve_item",
    }
    # The authorized intake action proceeds
    assert mixin._is_user_authorized(source) is True


def test_authz_mixin_intake_action_unauthorized_negative_denied(tmp_path, monkeypatch):
    """Negative test: an unauthorized intake action tool call is refused and denied."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from gateway.authz_mixin import GatewayAuthorizationMixin as AuthzMixin
    from gateway.session import SessionSource

    session_id = "session-authz-neg"
    mixin = AuthzMixin()
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id=session_id,
        user_id="webhook-user",
    )
    source.action_tool = "arbitrary_unauthorized_tool"
    source.action_args = {"item_id": 999}
    # Refused because no ledger claim exists
    assert mixin._is_user_authorized(source) is False


def test_authz_mixin_intake_action_fails_closed_on_missing_session_or_tool(tmp_path, monkeypatch):
    """Negative test: missing session or missing tool name fails closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from gateway.authz_mixin import GatewayAuthorizationMixin as AuthzMixin
    from gateway.session import SessionSource

    mixin = AuthzMixin()
    # Missing session
    source_no_session = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="",
        user_id="webhook-user",
    )
    source_no_session.action_tool = "approve_item"
    assert mixin._is_user_authorized(source_no_session) is False

    # Empty tool name
    assert mixin.check_intake_tool_authorization(source_no_session, "", {}) is False
    assert mixin.check_intake_tool_authorization(None, "approve_item", {}) is False
