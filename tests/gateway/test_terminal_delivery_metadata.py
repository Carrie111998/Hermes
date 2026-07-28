from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery_metadata import (
    TERMINAL_DELIVERY_METADATA_KEY,
    mark_terminal_delivery,
    project_terminal_delivery,
)
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.platforms.webhook import WebhookAdapter


def _webhook_adapter() -> tuple[WebhookAdapter, AsyncMock]:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            typing_indicator=False,
            extra={"routes": {}, "host": "127.0.0.1", "port": 0},
        )
    )
    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner.adapters = {Platform.TELEGRAM: target}
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="test"),
        }
    )
    adapter.gateway_runner = runner
    return adapter, target


def _seed_delivery(
    adapter: WebhookAdapter,
    *,
    chat_id: str = "webhook:synthetic:external-1",
    internal_id: str = "server-turn-1",
    correlation_id: str = "correlation-1",
) -> str:
    adapter._delivery_info[chat_id] = {
        "deliver": "telegram",
        "deliver_extra": {
            "chat_id": "callback-target",
            "correlation_id": correlation_id,
        },
        "_hermes_delivery_id": internal_id,
    }
    return chat_id


def _terminal_metadata(
    *,
    outcome: str = "success",
    correlation_id: str = "external-1",
    delivery_id: str = "external-1",
) -> dict:
    return mark_terminal_delivery(
        None,
        outcome=outcome,
        correlation_id=correlation_id,
        delivery_id=delivery_id,
    )


def test_terminal_metadata_projection_is_exact_and_bounded() -> None:
    metadata = mark_terminal_delivery(
        {"notify": True},
        outcome="success",
        correlation_id="c" * 300,
        delivery_id="d" * 300,
    )

    marker = project_terminal_delivery(metadata)

    assert marker is not None
    assert marker["outcome"] == "success"
    assert marker["correlation_id"].startswith("sha256:")
    assert marker["delivery_id"].startswith("sha256:")
    assert "notify" not in marker


@pytest.mark.parametrize(
    "marker",
    [
        None,
        "final",
        {},
        {
            "version": 1,
            "outcome": "success",
            "correlation_id": "c",
            "delivery_id": "d",
            "extra": "not allowed",
        },
        {
            "version": 1,
            "outcome": "interim",
            "correlation_id": "c",
            "delivery_id": "d",
        },
    ],
)
def test_malformed_terminal_metadata_fails_closed(marker) -> None:
    assert project_terminal_delivery({TERMINAL_DELIVERY_METADATA_KEY: marker}) is None


@pytest.mark.asyncio
async def test_multiple_interim_sends_then_final_project_one_terminal_marker() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)

    await adapter.send(chat_id, "interim one")
    await adapter.send(chat_id, "interim two", metadata={"notify": True})
    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(),
    )

    assert target.send.await_count == 3
    first, second, final = target.send.await_args_list
    assert first.kwargs["metadata"] is None
    assert second.kwargs["metadata"] is None
    marker = final.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker == {
        "version": 1,
        "outcome": "success",
        "correlation_id": "correlation-1",
        "delivery_id": "server-turn-1",
    }


@pytest.mark.asyncio
async def test_missing_server_delivery_identity_drops_terminal_marker() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    adapter._delivery_info[chat_id].pop("_hermes_delivery_id")

    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(),
    )

    assert target.send.await_args.kwargs["metadata"] is None


@pytest.mark.asyncio
async def test_successful_handler_marks_only_final_text() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name="webhook/synthetic",
        chat_type="webhook",
        user_id="webhook:synthetic",
        user_name="synthetic",
    )
    event = MessageEvent(
        text="synthetic request",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"synthetic": True},
        message_id="external-1",
    )
    adapter.set_message_handler(AsyncMock(return_value="complete response"))
    session_key = "webhook:synthetic:external-1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(event, session_key)

    target.send.assert_awaited_once()
    call = target.send.await_args
    assert call.args[:2] == ("callback-target", "complete response")
    marker = call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["outcome"] == "success"
    assert marker["delivery_id"] == "server-turn-1"


@pytest.mark.asyncio
async def test_terminal_handler_error_is_marked_once() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name="webhook/synthetic",
        chat_type="webhook",
        user_id="webhook:synthetic",
        user_name="synthetic",
    )
    event = MessageEvent(
        text="synthetic request",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"synthetic": True},
        message_id="external-1",
    )
    adapter.set_message_handler(
        AsyncMock(side_effect=RuntimeError("synthetic failure"))
    )
    session_key = "webhook:synthetic:external-1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(event, session_key)

    target.send.assert_awaited_once()
    call = target.send.await_args
    marker = call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["outcome"] == "error"
    assert marker["correlation_id"] == "correlation-1"
    assert marker["delivery_id"] == "server-turn-1"
    assert "synthetic failure" in call.args[1]


@pytest.mark.asyncio
async def test_distinct_accepted_turns_get_distinct_internal_delivery_ids(
    monkeypatch,
) -> None:
    generated = iter([
        SimpleNamespace(hex="server-turn-1"),
        SimpleNamespace(hex="server-turn-2"),
    ])
    monkeypatch.setattr(
        "gateway.platforms.webhook.uuid.uuid4",
        lambda: next(generated),
    )
    adapter, _target = _webhook_adapter()
    adapter._routes["synthetic"] = {
        "secret": "INSECURE_NO_AUTH",
        "prompt": "{text}",
        "deliver": "telegram",
        "deliver_extra": {
            "chat_id": "callback-target",
            "correlation_id": "{correlation_id}",
        },
    }
    adapter.handle_message = AsyncMock()

    def _request(delivery_id: str):
        body = '{"text":"same","correlation_id":"same-correlation"}'.encode("utf-8")
        request = MagicMock()
        request.headers = {"X-Request-ID": delivery_id}
        request.content_length = len(body)
        request.match_info = {"route_name": "synthetic"}
        request.method = "POST"
        request.read = AsyncMock(return_value=body)
        return request

    first = await adapter._handle_webhook(_request("external-1"))
    second = await adapter._handle_webhook(_request("external-2"))

    assert first.status == 202
    assert second.status == 202
    assert (
        adapter._delivery_info["webhook:synthetic:external-1"]["_hermes_delivery_id"]
        == "server-turn-1"
    )
    assert (
        adapter._delivery_info["webhook:synthetic:external-2"]["_hermes_delivery_id"]
        == "server-turn-2"
    )
