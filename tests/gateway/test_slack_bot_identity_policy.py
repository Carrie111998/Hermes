"""Regression tests for Slack peer-bot identity and routing policy."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.slack.adapter import SlackAdapter, _apply_yaml_config


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache"
    )
    config = PlatformConfig(enabled=True, token="***")
    instance = SlackAdapter(config)
    instance._app = MagicMock()
    instance._app.client = AsyncMock()
    instance._app.client.users_info = AsyncMock(
        return_value={
            "user": {
                "is_bot": False,
                "profile": {"display_name": "Test User"},
                "real_name": "Test User",
            }
        }
    )
    instance._bot_user_id = "U_BOT"
    instance._running = True
    instance.handle_message = AsyncMock()
    return instance


@pytest.mark.asyncio
async def test_admitted_bot_without_user_uses_stable_identity(adapter):
    adapter.config.extra.update(
        {
            "allow_bots": "all",
            "allowed_bots": "B_BACKEND",
            "bot_auto_response_channels": "C_BOTS",
        }
    )
    event = {
        "channel": "C_BOTS",
        "channel_type": "channel",
        "subtype": "bot_message",
        "user": "U_BACKEND_BOT",
        "bot_id": "B_BACKEND",
        "app_id": "A_BACKEND",
        "username": "backend-bot",
        "text": "Investigate this alert",
        "ts": "1700000000.000001",
    }

    await adapter._handle_slack_message(event)

    message = adapter.handle_message.await_args.args[0]
    assert message.source.user_id == "B_BACKEND"
    assert message.source.user_name == "backend-bot"
    assert message.source.is_bot is True
    assert message.source.role_authorized is False


@pytest.mark.asyncio
async def test_admitted_bot_in_auto_response_channel_bypasses_mentions_mode(adapter):
    adapter.config.extra.update(
        {
            "allow_bots": "mentions",
            "allowed_bots": "B_BACKEND",
            "bot_auto_response_channels": "C_BOTS",
        }
    )
    event = {
        "channel": "C_BOTS",
        "channel_type": "channel",
        "subtype": "bot_message",
        "bot_id": "B_BACKEND",
        "username": "backend-bot",
        "text": "Investigate this alert",
        "ts": "1700000000.000009",
    }

    await adapter._handle_slack_message(event)

    message = adapter.handle_message.await_args.args[0]
    assert message.source.user_id == "B_BACKEND"
    assert message.source.is_bot is True


@pytest.mark.asyncio
async def test_admitted_bot_passes_early_auth_with_exact_id(adapter):
    seen = []

    class Runner:
        def _is_user_authorized(self, source):
            seen.append(source)
            return source.is_bot and source.user_id == "B_BACKEND"

        async def handler(self, event):
            return None

    adapter._message_handler = Runner().handler
    adapter.config.extra.update(
        {
            "allow_bots": "all",
            "allowed_bots": "B_BACKEND",
            "bot_auto_response_channels": "C_BOTS",
        }
    )
    event = {
        "channel": "C_BOTS",
        "channel_type": "channel",
        "team": "T1",
        "subtype": "bot_message",
        "user": "U_BACKEND_BOT",
        "bot_id": "B_BACKEND",
        "username": "backend-bot",
        "text": "Investigate this alert",
        "ts": "1700000000.000004",
    }

    await adapter._handle_slack_message(event)

    assert len(seen) == 1
    assert seen[0].user_id == "B_BACKEND"
    assert seen[0].is_bot is True
    assert seen[0].role_authorized is False
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_early_unauthorized_rejection_marks_failure_with_x(adapter):
    class Runner:
        def _is_user_authorized(self, source):
            return False

        async def handler(self, event):
            return None

    adapter._message_handler = Runner().handler
    adapter._add_reaction = AsyncMock(return_value=True)
    event = {
        "channel": "C_SHARED",
        "channel_type": "channel",
        "team": "T1",
        "user": "U_DENIED",
        "client_msg_id": "client-1",
        "text": "hello",
        "ts": "1700000000.000005",
    }

    await adapter._handle_slack_message(event)

    adapter.handle_message.assert_not_awaited()
    adapter._add_reaction.assert_awaited_once_with(
        "C_SHARED", "1700000000.000005", "x", "T1"
    )


@pytest.mark.asyncio
async def test_unlisted_bot_identity_is_dropped(adapter):
    adapter.config.extra.update(
        {
            "allow_bots": "all",
            "allowed_bots": "B_BACKEND",
            "bot_auto_response_channels": "C_BOTS",
        }
    )
    event = {
        "channel": "C_BOTS",
        "channel_type": "channel",
        "subtype": "bot_message",
        "bot_id": "B_OTHER",
        "username": "other-bot",
        "text": "untrusted automation",
        "ts": "1700000000.000003",
    }

    await adapter._handle_slack_message(event)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_spoofed_allowed_display_name_does_not_grant_bot_access(adapter):
    adapter.config.extra.update(
        {
            "allow_bots": "all",
            "allowed_bots": "B_BACKEND",
            "bot_auto_response_channels": "C_BOTS",
        }
    )
    event = {
        "channel": "C_BOTS",
        "channel_type": "channel",
        "subtype": "bot_message",
        "bot_id": "B_OTHER",
        "username": "B_BACKEND",
        "text": "spoofed automation",
        "ts": "1700000000.000006",
    }

    await adapter._handle_slack_message(event)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_identityless_bot_message_uses_allow_bots_fallback(adapter):
    adapter.config.extra.update({"allow_bots": "all"})
    event = {
        "channel": "D_WORKFLOW",
        "channel_type": "im",
        "subtype": "bot_message",
        "text": "workflow request",
        "ts": "1700000000.000007",
    }

    await adapter._handle_slack_message(event)

    message = adapter.handle_message.await_args.args[0]
    assert message.source.user_id is None
    assert message.source.is_bot is True


@pytest.mark.asyncio
async def test_allowlist_selects_matching_stable_id(adapter):
    adapter.config.extra.update(
        {"allow_bots": "all", "allowed_bots": "A_BACKEND"}
    )
    event = {
        "channel": "D_APP",
        "channel_type": "im",
        "subtype": "bot_message",
        "user": "U_BACKEND_BOT",
        "bot_id": "B_BACKEND",
        "app_id": "A_BACKEND",
        "text": "app request",
        "ts": "1700000000.000008",
    }

    await adapter._handle_slack_message(event)

    message = adapter.handle_message.await_args.args[0]
    assert message.source.user_id == "A_BACKEND"
    assert message.source.is_bot is True


def test_yaml_bot_policy_is_seeded_without_multiplex_env_leak(monkeypatch):
    from agent import secret_scope

    for name in (
        "SLACK_ALLOW_BOTS",
        "SLACK_ALLOWED_BOTS",
        "SLACK_BOT_AUTO_RESPONSE_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({})
    try:
        seeded = _apply_yaml_config(
            {},
            {
                "extra": {
                    "allow_bots": "all",
                    "allowed_bots": ["B_PROFILE"],
                    "bot_auto_response_channels": ["C_PROFILE"],
                }
            },
        )
    finally:
        secret_scope.reset_secret_scope(token)

    assert seeded == {
        "allow_bots": "all",
        "allowed_bots": "B_PROFILE",
        "bot_auto_response_channels": "C_PROFILE",
    }
    assert os.getenv("SLACK_ALLOW_BOTS") is None
    assert os.getenv("SLACK_ALLOWED_BOTS") is None
    assert os.getenv("SLACK_BOT_AUTO_RESPONSE_CHANNELS") is None


def test_final_gateway_auth_uses_receiving_adapter_bot_policy(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOW_BOTS", "all")
    monkeypatch.setenv("SLACK_ALLOWED_BOTS", "B_PROFILE_A")
    profile_a = SlackAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"allow_bots": "all", "allowed_bots": "B_PROFILE_A"},
        )
    )
    profile_b = SlackAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"allow_bots": "all", "allowed_bots": "B_PROFILE_B"},
        )
    )
    runner = object.__new__(GatewayRunner)
    runner.__dict__["pairing_store"] = SimpleNamespace(
        is_approved=lambda *_a, **_kw: False
    )
    runner.adapters = {Platform.SLACK: profile_a}
    runner._profile_adapters = {"profile-b": {Platform.SLACK: profile_b}}

    source_a = profile_a.build_source(
        chat_id="C1", chat_type="group", user_id="B_PROFILE_A", is_bot=True
    )
    source_b = profile_b.build_source(
        chat_id="C1", chat_type="group", user_id="B_PROFILE_B", is_bot=True
    )
    wrong_for_b = profile_b.build_source(
        chat_id="C1", chat_type="group", user_id="B_PROFILE_A", is_bot=True
    )

    assert runner._is_user_authorized(source_a) is True
    assert runner._is_user_authorized(source_b) is True
    assert runner._is_user_authorized(wrong_for_b) is False
