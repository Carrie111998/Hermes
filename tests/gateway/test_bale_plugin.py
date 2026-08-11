"""Behavior tests for the Bale gateway platform plugin."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig


@pytest.fixture
def bale_plugin():
    """Load the real Bale plugin after asserting that it exists."""
    module_name = "plugins.platforms.bale.adapter"
    assert importlib.util.find_spec(module_name) is not None, "Bale platform plugin is missing"
    return importlib.import_module(module_name)


class _RegistrationContext:
    """Capture one platform registration at the plugin boundary."""

    def __init__(self) -> None:
        self.registration: dict | None = None

    def register_platform(self, **kwargs) -> None:
        """Store the exact platform contract supplied by the plugin."""
        self.registration = kwargs


def test_register_exposes_independent_bale_gateway_contract(bale_plugin):
    """Bale must use its own credentials, authorization, and Persian hint."""
    ctx = _RegistrationContext()

    bale_plugin.register(ctx)

    registration = ctx.registration
    assert registration is not None
    assert registration["name"] == "bale"
    assert registration["label"] == "Bale (بله)"
    assert registration["required_env"] == ["BALE_BOT_TOKEN"]
    assert registration["allowed_users_env"] == "BALE_ALLOWED_USERS"
    assert registration["allow_all_env"] == "BALE_ALLOW_ALL_USERS"
    assert registration["cron_deliver_env_var"] == "BALE_HOME_CHANNEL"
    assert "Persian" in registration["platform_hint"]
    assert "unless the user requests another language" in registration["platform_hint"]


def test_env_enablement_seeds_endpoints_and_home_channel(monkeypatch, bale_plugin):
    """A BALE_BOT_TOKEN-only install must auto-enable with Bale API roots."""
    monkeypatch.setenv("BALE_BOT_TOKEN", "test-token")
    monkeypatch.setenv("BALE_HOME_CHANNEL", "123456")
    monkeypatch.setenv("BALE_HOME_CHANNEL_NAME", "تیم پشتیبانی")

    seed = bale_plugin._env_enablement()

    assert seed == {
        "base_url": "https://tapi.bale.ai/bot",
        "base_file_url": "https://tapi.bale.ai/file/bot",
        "home_channel": {
            "chat_id": "123456",
            "name": "تیم پشتیبانی",
        },
    }


def test_adapter_resolves_env_token_without_storing_it_in_extras(
    monkeypatch, bale_plugin
):
    """Env-only setup must connect without copying its secret into extras."""
    monkeypatch.setenv("BALE_BOT_TOKEN", "from-env")
    config = PlatformConfig(enabled=True, token=None, extra={})

    adapter = bale_plugin.BaleAdapter(config)

    assert adapter.config.token == "from-env"
    assert "token" not in adapter.config.extra


def test_env_enablement_requires_token(monkeypatch, bale_plugin):
    """A home channel without a Bale credential must not enable the platform."""
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    monkeypatch.setenv("BALE_HOME_CHANNEL", "123456")

    assert bale_plugin._env_enablement() is None


def test_yaml_config_keeps_bale_roots_and_supported_telegram_options(bale_plugin):
    """Bale YAML extras may tune shared behavior but cannot replace API roots."""
    extras = bale_plugin._apply_yaml_config(
        {},
        {
            "disable_link_previews": True,
            "extra": {
                "reply_to_mode": "off",
                "base_url": "https://attacker.invalid/bot",
                "base_file_url": "https://attacker.invalid/file",
            },
        },
    )

    assert extras == {
        "disable_link_previews": True,
        "reply_to_mode": "off",
        "base_url": "https://tapi.bale.ai/bot",
        "base_file_url": "https://tapi.bale.ai/file/bot",
        "rich_messages": False,
        "rich_drafts": False,
        "disable_fallback_ips": True,
    }


def test_adapter_uses_bale_identity_and_disables_undocumented_extensions(
    monkeypatch, bale_plugin
):
    """Gateway sessions must be keyed as Bale and avoid Telegram-only APIs."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    config = PlatformConfig(enabled=True, token="test-token", extra={})

    adapter = bale_plugin.BaleAdapter(config)

    assert adapter.platform.value == "bale"
    assert adapter.name == "Bale"
    assert adapter.config.extra["base_url"] == "https://tapi.bale.ai/bot"
    assert adapter.config.extra["base_file_url"] == "https://tapi.bale.ai/file/bot"
    assert adapter._rich_messages_enabled is False
    assert adapter._rich_drafts_enabled is False
    assert adapter.config.extra["disable_fallback_ips"] is True
    assert adapter._reactions_enabled() is False


def test_adapter_skips_telegram_only_post_connect_calls(bale_plugin):
    """Bale startup must not call undocumented Telegram command-menu APIs."""

    class _Bot:
        async def set_my_commands(self, *_args, **_kwargs) -> None:
            """Fail if Bale attempts Telegram's command-menu extension."""
            raise AssertionError("set_my_commands must not be called for Bale")

    adapter = bale_plugin.BaleAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._bot = _Bot()

    asyncio.run(adapter._run_post_connect_housekeeping())


def test_adapter_does_not_inherit_telegram_webhook_configuration(
    monkeypatch, bale_plugin
):
    """A concurrent Telegram webhook must not switch Bale away from polling."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram")
    adapter = bale_plugin.BaleAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )

    assert adapter._webhook_url() == ""


def test_adapter_does_not_inherit_telegram_proxy_configuration(
    monkeypatch, bale_plugin
):
    """A Telegram-specific proxy must not be reused for Bale traffic."""
    monkeypatch.setenv("TELEGRAM_PROXY", "http://127.0.0.1:9999")
    adapter = bale_plugin.BaleAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )

    assert adapter._proxy_url(["tapi.bale.ai"]) is None


def test_adapter_builds_bale_session_sources(bale_plugin):
    """Inbound Bale messages must not collide with Telegram session keys."""
    adapter = bale_plugin.BaleAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42, username="saleh", full_name="Saleh"),
        sender_chat=None,
        chat=SimpleNamespace(id=100, type="private", is_forum=False),
        message_thread_id=None,
        is_topic_message=False,
    )

    source = adapter._source_from_message_for_auth(message)

    assert source.platform.value == "bale"
    assert source.chat_id == "100"
    assert source.user_id == "42"


def test_connected_check_accepts_configured_token(monkeypatch, bale_plugin):
    """Gateway status must report Bale configured from config or BALE_BOT_TOKEN."""
    monkeypatch.delenv("BALE_BOT_TOKEN", raising=False)
    assert bale_plugin._is_connected(PlatformConfig(token="from-config")) is True
    assert bale_plugin._is_connected(PlatformConfig(token="")) is False

    monkeypatch.setenv("BALE_BOT_TOKEN", "from-env")
    assert bale_plugin._is_connected(PlatformConfig(token=None)) is True


def test_standalone_send_posts_to_bale_and_returns_message_id(monkeypatch, bale_plugin):
    """Out-of-process delivery must call Bale sendMessage with the expected body."""
    captured: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            """Represent a successful Bale HTTP response."""

        def json(self) -> dict:
            """Return the documented Telegram-compatible success envelope."""
            return {"ok": True, "result": {"message_id": 987}}

    class _Client:
        async def __aenter__(self):
            """Open the fake client context."""
            return self

        async def __aexit__(self, *_args) -> None:
            """Close the fake client context."""

        async def post(self, url: str, *, json: dict) -> _Response:
            """Capture the outbound request without touching the network."""
            captured.update(url=url, json=json)
            return _Response()

    monkeypatch.setattr(bale_plugin.httpx, "AsyncClient", lambda **_kwargs: _Client())
    config = SimpleNamespace(token="test-token", extra={})

    result = asyncio.run(bale_plugin._standalone_send(config, "123", "سلام"))

    assert captured == {
        "url": "https://tapi.bale.ai/bottest-token/sendMessage",
        "json": {"chat_id": "123", "text": "سلام"},
    }
    assert result == {"success": True, "message_id": "987"}


def test_standalone_send_rejects_media_until_bale_transport_supports_it(bale_plugin):
    """Cron delivery must fail clearly rather than silently dropping attachments."""
    config = SimpleNamespace(token="test-token", extra={})

    result = asyncio.run(
        bale_plugin._standalone_send(config, "123", "گزارش", media_files=["report.pdf"])
    )

    assert result == {
        "error": "Bale standalone delivery does not support media attachments yet"
    }
