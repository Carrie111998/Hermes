"""Gateway auto-resume must defer webhook sessions to their exact ledger."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_auth import WebhookLocalBypassReceipt
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.session import SessionSource, SessionStore
from tests.gateway.restart_test_helpers import RestartTestAdapter


@pytest.mark.asyncio
async def test_webhook_resume_pending_is_not_claimed_by_generic_auto_resume(
    tmp_path, monkeypatch
):
    """Ledger-owned webhook recovery must not touch the generic breaker."""

    from gateway import restart_loop_guard
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(sessions_dir=tmp_path / "sessions")
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = SessionStore(config.sessions_dir, config)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._sessions = {}
    runner._background_tasks = set()

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": {}},
        )
    )
    adapter.gateway_runner = runner
    runner.adapters[Platform.WEBHOOK] = adapter

    raw_body = b'{"event":"resume-proof"}'
    route = WebhookRouteConfig.bind(
        "resume-proof",
        {"provider": "generic", "profile": "default"},
        headers={},
        request_profile="default",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="resume-proof-trace",
    )
    source = adapter._source_for_envelope(envelope)
    entry = runner.session_store.get_or_create_session(source)
    assert runner.session_store.mark_resume_pending(
        entry.session_key,
        reason="restart_interrupted",
    )
    assert entry.resume_pending is True
    assert entry.origin is source
    assert source.platform is Platform.WEBHOOK
    assert adapter.allows_automatic_session_resume is False

    handle_message = AsyncMock(
        side_effect=AssertionError("generic auto-resume invoked webhook execution")
    )
    adapter.handle_message = handle_message
    persist_active_agents = MagicMock()
    runner._persist_active_agents = persist_active_agents
    restart_check = MagicMock(return_value=False)
    monkeypatch.setattr(restart_loop_guard, "check_and_record", restart_check)

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 0
    assert runner._peek_session_state(entry.session_key) is None
    assert runner._is_session_running(entry.session_key) is False
    assert runner._background_tasks == set()
    handle_message.assert_not_awaited()
    persist_active_agents.assert_not_called()
    restart_check.assert_not_called()
    retained = runner.session_store._entries[entry.session_key]
    assert retained.resume_pending is True
    assert retained.resume_reason == "restart_interrupted"


@pytest.mark.asyncio
async def test_mixed_webhook_and_telegram_resume_checks_only_generic_work(
    tmp_path, monkeypatch
):
    """A webhook marker cannot suppress or inflate an eligible Telegram pass."""

    from gateway import restart_loop_guard
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(sessions_dir=tmp_path / "sessions")
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = SessionStore(config.sessions_dir, config)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._sessions = {}
    runner._background_tasks = set()
    runner._is_user_authorized = lambda _source: True
    runner._persist_active_agents = MagicMock()

    webhook_adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": {}},
        )
    )
    webhook_adapter.gateway_runner = runner
    webhook_adapter.handle_message = AsyncMock(
        side_effect=AssertionError("generic auto-resume invoked webhook execution")
    )
    runner.adapters[Platform.WEBHOOK] = webhook_adapter

    raw_body = b'{"event":"mixed-resume-proof"}'
    route = WebhookRouteConfig.bind(
        "mixed-resume-proof",
        {"provider": "generic", "profile": "default"},
        headers={},
        request_profile="default",
    )
    envelope = WebhookEnvelope.from_receipt(
        WebhookLocalBypassReceipt._issue(route, raw_body, {}),
        raw_body=raw_body,
        media_type="application/json",
        trace_id="mixed-resume-proof-trace",
    )
    webhook_source = webhook_adapter._source_for_envelope(envelope)
    webhook_entry = runner.session_store.get_or_create_session(webhook_source)
    assert runner.session_store.mark_resume_pending(
        webhook_entry.session_key,
        reason="restart_interrupted",
    )

    telegram_adapter = RestartTestAdapter()
    telegram_adapter.handle_message = AsyncMock(return_value=None)
    runner.adapters[Platform.TELEGRAM] = telegram_adapter
    telegram_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="mixed-telegram-chat",
        user_id="telegram-owner",
    )
    telegram_entry = runner.session_store.get_or_create_session(telegram_source)
    assert runner.session_store.mark_resume_pending(
        telegram_entry.session_key,
        reason="restart_interrupted",
    )

    restart_check = MagicMock(return_value=False)
    monkeypatch.setattr(restart_loop_guard, "check_and_record", restart_check)

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    restart_check.assert_called_once()
    webhook_adapter.handle_message.assert_not_awaited()
    telegram_adapter.handle_message.assert_awaited_once()
    resumed_event = telegram_adapter.handle_message.await_args.args[0]
    assert resumed_event.source is telegram_source
    retained = runner.session_store._entries[webhook_entry.session_key]
    assert retained.resume_pending is True
    assert retained.resume_reason == "restart_interrupted"
