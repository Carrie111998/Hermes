"""Auto-TTS must not widen a webhook's one durable final carrier."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.platforms.webhook_ledger import (
    OperationState,
    WebhookOperationLedger,
)
from gateway.session import SessionEntry, build_session_key


def _webhook_adapter(tmp_path: Path) -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "port": 0,
            "routes": {
                "alerts": {
                    "secret": _INSECURE_NO_AUTH,
                    "provider": "github",
                    "prompt": "Handle: {message}",
                    "deliver": "telegram",
                    "deliver_extra": {"chat_id": "target-chat"},
                }
            },
        },
    )
    adapter = WebhookAdapter(config)
    adapter._operation_ledger = WebhookOperationLedger(
        tmp_path / "webhook-authority.db"
    )
    return adapter


def _gateway_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter: WebhookAdapter,
    target: MagicMock,
) -> gateway_run.GatewayRunner:
    from hermes_constants import get_hermes_home

    # Route publication freezes grants from the physical profile config. Keep
    # this real-runner fixture on that production path instead of relying only
    # on the in-process load_config double below.
    (get_hermes_home() / "config.yaml").write_text(
        "voice:\n  auto_tts: true\n",
        encoding="utf-8",
    )
    config = GatewayConfig(
        platforms={
            Platform.WEBHOOK: adapter.config,
            Platform.TELEGRAM: target.config,
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {
        Platform.WEBHOOK: adapter,
        Platform.TELEGRAM: target,
    }
    runner._profile_adapters = {}
    runner._voice_mode = {}
    runner._running = True
    runner._startup_restore_in_progress = False
    runner._draining = False
    runner._external_drain_active = False
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._turn_leases = None
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._get_guild_id = lambda _event: None
    runner._set_session_env = lambda _context: None
    runner._clear_session_env = lambda _tokens: None
    runner._clear_restart_failure_count = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._drain_watch_notifications = AsyncMock()
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    # Use the same GatewayRunner method that production uses to populate the
    # adapter's global voice.auto_tts default.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"voice": {"auto_tts": True}},
    )
    runner._sync_voice_mode_state_to_adapter(adapter)
    assert adapter._auto_tts_default is True

    return runner


def _app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


@pytest.mark.asyncio
async def test_global_auto_tts_cannot_claim_webhook_final_before_base_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Drive GatewayRunner's real finalization into Base's real send funnel."""

    adapter = _webhook_adapter(tmp_path)
    target = MagicMock()
    target.platform = Platform.TELEGRAM
    target.config = PlatformConfig(enabled=True, token="test-token")
    target.send = AsyncMock(
        return_value=SendResult(success=True, message_id="target-message")
    )
    runner = _gateway_runner(monkeypatch, tmp_path, adapter, target)
    adapter.gateway_runner = runner

    captured = []

    async def capture(event):
        captured.append(event)

    # First cross the real HTTP admission/prepare boundary. Processing is
    # deliberately captured so the remainder can be driven synchronously.
    adapter.handle_message = capture
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "build the report"},
            headers={"X-GitHub-Delivery": "auto-tts-authority"},
        )
        assert response.status == 202
    await asyncio.sleep(0)
    assert len(captured) == 1
    event = captured[0]

    now = datetime.now()
    session_key = build_session_key(event.source)
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="webhook-auto-tts-session",
        created_at=now - timedelta(minutes=1),
        updated_at=now,
        origin=event.source,
        platform=Platform.WEBHOOK,
        chat_type="webhook",
    )
    history = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "acknowledged"},
    ]
    runner.session_store = MagicMock()
    runner.session_store.config = runner.config
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = history
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.has_platform_message_id.return_value = False

    raw_final = "**Exact durable final.**\n\nMEDIA:/tmp/webhook-auto-tts-attachment.mp3"
    expected_final = (
        "**Exact durable final.**\n\n"
        "⚠️ Local attachments were omitted from webhook delivery."
    )
    prepare_trap = MagicMock(wraps=adapter.prepare_ledger_owned_final_content)
    adapter.prepare_ledger_owned_final_content = prepare_trap
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": raw_final,
            "messages": [
                *history,
                {"role": "user", "content": event.text},
                {"role": "assistant", "content": raw_final},
            ],
            "history_offset": len(history),
            "tools": [],
            "last_prompt_tokens": 0,
            "api_calls": 1,
            "failed": False,
            "agent_persisted": False,
        }
    )

    # If either generic auto-TTS stage is reached, allow the historical bug
    # to materialize: synthesis creates a file and Base's inherited voice
    # fallback would try to send its audio-error notice through webhook.send.
    def fake_tts(*, text: str, output_path: str) -> str:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake audio")
        return json.dumps({"success": True, "file_path": output_path})

    tts_trap = MagicMock(side_effect=fake_tts)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", tts_trap)
    voice_fallback_trap = AsyncMock(wraps=adapter.send_voice)
    adapter.send_voice = voice_fallback_trap

    async def gateway_handler(inbound_event):
        return await runner._handle_message_with_agent(
            inbound_event,
            inbound_event.source,
            session_key,
            1,
        )

    adapter.set_message_handler(gateway_handler)
    await adapter._process_message_background(event, session_key)

    assert adapter._should_auto_tts_for_chat(event.source.chat_id) is True
    assert runner._should_send_voice_reply(event, raw_final, []) is False
    tts_trap.assert_not_called()
    voice_fallback_trap.assert_not_awaited()
    prepare_trap.assert_called_once_with(raw_final, session_key=session_key)
    target.send.assert_awaited_once_with(
        "target-chat",
        expected_final,
        metadata=None,
    )

    settled = adapter._operation_ledger.lookup_session(event.source.chat_id)
    assert settled is not None
    assert settled.state is OperationState.SETTLED
    assert settled.delivery is not None
    assert settled.delivery.content == expected_final
    assert dict(settled.delivery.carrier) == {"v": 1, "kind": "agent_final"}


@pytest.mark.asyncio
async def test_ledger_owned_adapter_inherited_voice_fallback_never_sends_text(
    tmp_path: Path,
):
    """Defense in depth: Base's fallback cannot mutate a ledger final."""

    adapter = _webhook_adapter(tmp_path)
    adapter.send = AsyncMock(
        side_effect=AssertionError("voice fallback attempted a webhook text send")
    )

    result = await BasePlatformAdapter.send_voice(
        adapter,
        chat_id="webhook:authority",
        audio_path="/tmp/undeliverable.mp3",
        metadata={"notify": True},
    )

    assert result.success is False
    assert result.error == "Voice delivery is unavailable for ledger-owned finals"
    adapter.send.assert_not_awaited()
