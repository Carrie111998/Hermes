import hashlib
import hmac
import json
import stat
import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.webhook import WebhookAdapter

SECRET = "callback-secret-long-enough-for-production-tests"
ROUTE = "ridge-hill-utility-investigation"
DELIVERY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def callback_config(url: str = "http://127.0.0.1:8765/callback") -> dict:
    return {
        "url": url,
        "secret": SECRET,
        "include_payload_fields": [
            "investigation_id",
            "review_id",
            "discord_thread_id",
        ],
        "max_attempts": 2,
        "timeout_seconds": 2,
    }


def make_adapter(callback: dict | None = None) -> WebhookAdapter:
    route = {"secret": "incoming-secret", "prompt": "x", "deliver": "log"}
    if callback is not None:
        route["completion_callback"] = callback
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": {ROUTE: route}},
        )
    )


def make_event(adapter: WebhookAdapter) -> MessageEvent:
    chat_id = f"webhook:{ROUTE}:{DELIVERY}"
    adapter._delivery_info[chat_id] = {
        "completion_callback": callback_config(),
        "completion_correlation": {
            "investigation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "review_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "discord_thread_id": "1530677153690161303",
        },
        "delivery_id": DELIVERY,
        "route_name": ROUTE,
    }
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name=f"webhook/{ROUTE}",
        chat_type="webhook",
        user_id=f"webhook:{ROUTE}",
        user_name=ROUTE,
    )
    return MessageEvent(
        text="investigate",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={},
        message_id=DELIVERY,
    )


def test_completion_callback_config_fails_closed():
    adapter = make_adapter()
    with pytest.raises(ValueError, match="too short"):
        adapter._completion_callback_config(
            ROUTE,
            {"completion_callback": {**callback_config(), "secret": "short"}},
        )
    with pytest.raises(ValueError, match="URL"):
        adapter._completion_callback_config(
            ROUTE,
            {
                "completion_callback": callback_config(
                    "http://user:password@127.0.0.1/callback"
                )
            },
        )


@pytest.mark.asyncio
async def test_completion_callback_is_hmac_signed_and_retries_after_500():
    requests = []

    async def receive(request: web.Request) -> web.Response:
        raw = await request.read()
        timestamp = request.headers["X-Webhook-Timestamp"]
        expected = hmac.new(
            SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        assert hmac.compare_digest(
            request.headers["X-Webhook-Signature-V2"], expected
        )
        requests.append(json.loads(raw))
        return web.Response(status=500 if len(requests) == 1 else 200)

    app = web.Application()
    app.router.add_post("/callback", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    adapter = make_adapter()
    event = make_event(adapter)
    adapter._delivery_info[event.source.chat_id]["completion_callback"] = (
        callback_config(f"http://127.0.0.1:{port}/callback")
    )
    body = adapter._completion_callback_body(event, "completed")
    try:
        assert await adapter._send_completion_callback(event.source.chat_id, body)
    finally:
        await runner.cleanup()
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[1]["status"] == "completed"
    assert requests[1]["delivery_id"] == DELIVERY


@pytest.mark.asyncio
async def test_final_callback_is_spooled_then_removed_after_ack(tmp_path):
    adapter = make_adapter(callback_config())
    event = make_event(adapter)
    adapter._callback_outbox_dir = lambda: tmp_path / "outbox"
    adapter._send_completion_callback = AsyncMock(return_value=True)
    adapter._end_webhook_session = AsyncMock()

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    sent = adapter._send_completion_callback.await_args.args[1]
    assert sent["status"] == "completed"
    assert not list((tmp_path / "outbox").glob("*.json"))
    adapter._end_webhook_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_callback_survives_and_replays_after_restart(tmp_path):
    adapter = make_adapter(callback_config())
    event = make_event(adapter)
    adapter._callback_outbox_dir = lambda: tmp_path / "outbox"
    adapter._send_completion_callback = AsyncMock(return_value=False)
    adapter._end_webhook_session = AsyncMock()

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    files = list((tmp_path / "outbox").glob("*.json"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    entry = json.loads(files[0].read_text())
    assert entry["body"]["status"] == "failed"
    assert entry["body"]["error_code"] == "processing_failed"
    assert "secret" not in entry and SECRET not in files[0].read_text()

    restarted = make_adapter(callback_config())
    restarted._callback_outbox_dir = lambda: tmp_path / "outbox"
    restarted._send_completion_callback = AsyncMock(return_value=True)
    await restarted._replay_completion_callbacks()

    assert not list((tmp_path / "outbox").glob("*.json"))
    replayed = restarted._send_completion_callback.await_args.args[1]
    assert replayed == entry["body"]


@pytest.mark.asyncio
async def test_retained_callbacks_retry_periodically_without_restart():
    adapter = make_adapter(callback_config())
    adapter._callback_replay_interval = 0.01
    adapter._replay_completion_callbacks = AsyncMock()
    task = asyncio.create_task(adapter._completion_callback_replay_loop())
    try:
        for _ in range(100):
            if adapter._replay_completion_callbacks.await_count >= 2:
                break
            await asyncio.sleep(0.002)
        assert adapter._replay_completion_callbacks.await_count >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
