"""Durable, redacted terminal outcomes for failed webhook agent runs."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.platforms.webhook_ledger import OperationState, TargetState
from gateway.platforms.webhook_terminal import (
    WebhookTerminalOutcome,
    terminal_outcome_carrier,
    terminal_outcome_notice,
)


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _drain(adapter: WebhookAdapter) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while adapter._background_tasks and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    assert not adapter._background_tasks


def test_terminal_error_contract_is_typed_and_privacy_bounded():
    outcome = WebhookTerminalOutcome.ERROR

    assert terminal_outcome_notice(outcome) == (
        "Webhook processing failed before a final response was produced."
    )
    assert terminal_outcome_carrier(outcome) == {
        "v": 1,
        "kind": "terminal_outcome",
        "outcome": "error",
    }
    with pytest.raises(TypeError):
        terminal_outcome_notice("provider token=secret")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        terminal_outcome_carrier("error")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_agent_exception_stages_redacted_error_before_reconciliation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "events": {
                        "provider": "github",
                        "secret": _INSECURE_NO_AUTH,
                        "deliver": "telegram",
                        "deliver_extra": {"chat_id": "alert-channel"},
                    }
                },
            },
        )
    )
    target = SimpleNamespace(
        send=AsyncMock(
            return_value=SendResult(success=True, message_id="error-notice-1")
        )
    )
    runner = SimpleNamespace(
        adapters={Platform.WEBHOOK: adapter, Platform.TELEGRAM: target},
        _profile_adapters={},
        config=GatewayConfig(platforms={}, multiplex_profiles=False),
        _running=True,
    )
    adapter.gateway_runner = runner
    captured = []
    sensitive_error = "provider failed with token=super-secret-value"

    async def fail_agent(event):
        captured.append(event)
        raise RuntimeError(sensitive_error)

    adapter._message_handler = fail_agent
    body = {"action": "opened", "number": 42}
    headers = {"X-GitHub-Delivery": "observed-error-delivery"}

    async with TestClient(TestServer(_app(adapter))) as client:
        accepted = await client.post("/webhooks/events", json=body, headers=headers)
        await _drain(adapter)
        duplicate = await client.post("/webhooks/events", json=body, headers=headers)
        duplicate_payload = await duplicate.json()

    assert accepted.status == 202
    assert duplicate.status == 200
    assert duplicate_payload["status"] == "duplicate"
    assert len(captured) == 1
    target.send.assert_awaited_once()
    chat_id, content = target.send.await_args.args[:2]
    assert chat_id == "alert-channel"
    assert content == terminal_outcome_notice(WebhookTerminalOutcome.ERROR)
    assert sensitive_error not in content

    settled = adapter._operation_ledger.lookup_session(
        captured[0].webhook_authority.session_key
    )
    assert settled is not None
    assert settled.state is OperationState.SETTLED
    assert settled.target_state is TargetState.CONFIRMED
    assert settled.delivery is not None
    assert dict(settled.delivery.carrier) == terminal_outcome_carrier(
        WebhookTerminalOutcome.ERROR
    )
    assert sensitive_error not in settled.delivery.content
    assert sensitive_error not in json.dumps(dict(settled.delivery.carrier))
