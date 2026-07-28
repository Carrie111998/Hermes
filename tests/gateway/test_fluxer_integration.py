"""Local end-to-end tests for the Fluxer REST + Gateway integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, TypedDict

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent


class _StubState(TypedDict):
    identify: dict[str, Any] | None
    outbound: dict[str, Any] | None
    authorization: list[str | None]
    api_url: str
    gateway_url: str


@asynccontextmanager
async def _fluxer_stub() -> AsyncIterator[tuple[_StubState, asyncio.Event]]:
    state: _StubState = {
        "identify": None,
        "outbound": None,
        "authorization": [],
        "api_url": "",
        "gateway_url": "",
    }
    inbound_sent = asyncio.Event()

    async def users_me(request: web.Request) -> web.Response:
        state["authorization"].append(request.headers.get("Authorization"))
        return web.json_response({"id": "bot-1", "username": "Hermes"})

    async def gateway_bot(request: web.Request) -> web.Response:
        state["authorization"].append(request.headers.get("Authorization"))
        return web.json_response({"url": state["gateway_url"]})

    async def channel(request: web.Request) -> web.Response:
        return web.json_response({"id": request.match_info["channel_id"], "type": 1})

    async def create_message(request: web.Request) -> web.Response:
        state["authorization"].append(request.headers.get("Authorization"))
        state["outbound"] = await request.json()
        return web.json_response({"id": "outbound-1"})

    async def gateway(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 60_000}})
        identify = await ws.receive_json(timeout=5)
        state["identify"] = identify
        await ws.send_json({
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {
                "session_id": "session-1",
                "user": {"id": "bot-1", "username": "Hermes"},
            },
        })
        await ws.send_json({
            "op": 0,
            "s": 2,
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "inbound-1",
                "channel_id": "dm-1",
                "content": "hello from Fluxer",
                "type": 0,
                "author": {"id": "user-1", "username": "Kait", "bot": False},
                "attachments": [],
                "mentions": [],
            },
        })
        inbound_sent.set()
        async for _message in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/v1/users/@me", users_me)
    app.router.add_get("/v1/gateway/bot", gateway_bot)
    app.router.add_get("/v1/channels/{channel_id}", channel)
    app.router.add_post("/v1/channels/{channel_id}/messages", create_message)
    app.router.add_get("/gateway", gateway)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    addresses = runner.addresses
    assert addresses
    port = addresses[0][1]
    state["api_url"] = f"http://127.0.0.1:{port}/v1"
    state["gateway_url"] = f"ws://127.0.0.1:{port}/gateway"
    try:
        yield state, inbound_sent
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_real_rest_and_gateway_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)

    async with _fluxer_stub() as (state, inbound_sent):
        from plugins.platforms.fluxer.adapter import FluxerAdapter

        adapter = FluxerAdapter(
            PlatformConfig(
                enabled=True,
                token="integration-token",
                extra={"api_url": state["api_url"]},
            )
        )
        received = asyncio.Event()
        events: list[MessageEvent] = []

        async def capture(event: MessageEvent) -> None:
            events.append(event)
            received.set()

        setattr(adapter, "handle_message", capture)
        try:
            assert await adapter.connect() is True
            await asyncio.wait_for(inbound_sent.wait(), timeout=5)
            await asyncio.wait_for(received.wait(), timeout=5)

            result = await adapter.send("dm-1", "hello back", reply_to="inbound-1")
            assert result.success is True
            assert result.message_id == "outbound-1"
        finally:
            await adapter.disconnect()

    assert state["authorization"] == [
        "Bot integration-token",
        "Bot integration-token",
        "Bot integration-token",
    ]
    identify = state["identify"]
    assert identify is not None
    assert identify["op"] == 2
    assert identify["d"]["token"] == "integration-token"
    assert events[0].text == "hello from Fluxer"
    assert events[0].source.chat_type == "dm"
    outbound = state["outbound"]
    assert outbound is not None
    assert outbound["content"] == "hello back"
    assert outbound["message_reference"]["message_id"] == "inbound-1"
