"""Compatibility contracts for opt-in Discord voice engagement auto-join."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.discord.adapter import DiscordAdapter, _GATE_ENV_KEYS, _apply_yaml_config


def _adapter(extra=None, *, snapshot=None):
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="test", extra=dict(extra or {}))
    adapter._gate_env_snapshot = {
        key: (snapshot or {}).get(key, "") for key in _GATE_ENV_KEYS
    }
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._on_voice_joined = None
    return adapter


def test_yaml_voice_auto_join_is_seeded_into_profile_extra(monkeypatch):
    monkeypatch.delenv("DISCORD_VOICE_AUTO_JOIN", raising=False)

    seeded = _apply_yaml_config({}, {"voice_auto_join": True})

    assert seeded["voice_auto_join"] is True


def test_real_gateway_loader_preserves_discord_voice_auto_join(tmp_path, monkeypatch):
    from gateway import config as gateway_config

    (tmp_path / "config.yaml").write_text(
        "discord:\n  enabled: true\n  voice_auto_join: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_config, "get_hermes_home", lambda: tmp_path)

    loaded = gateway_config.load_gateway_config()

    assert loaded.platforms[Platform.DISCORD].extra["voice_auto_join"] is True


def test_profile_voice_auto_join_setting_precedes_legacy_env_snapshot():
    adapter = _adapter(
        {"voice_auto_join": False},
        snapshot={"DISCORD_VOICE_AUTO_JOIN": "true"},
    )

    assert adapter._voice_auto_join_enabled() is False


def test_legacy_voice_auto_join_env_snapshot_remains_supported():
    adapter = _adapter(snapshot={"DISCORD_VOICE_AUTO_JOIN": "true"})

    assert adapter._voice_auto_join_enabled() is True


@pytest.mark.asyncio
async def test_maybe_auto_join_binds_text_source_and_notifies_runner():
    adapter = _adapter({"voice_auto_join": True})
    voice_channel = SimpleNamespace(name="Workshop")
    adapter.get_user_voice_channel = AsyncMock(return_value=voice_channel)
    adapter.join_voice_channel = AsyncMock(return_value=True)
    adapter.is_in_voice_channel = MagicMock(return_value=False)
    adapter._reset_voice_timeout = MagicMock()
    joined = MagicMock()
    adapter._on_voice_joined = joined
    source = {"platform": "discord", "chat_id": "456"}

    result = await adapter.maybe_auto_join_voice(
        guild_id=123,
        user_id="789",
        text_channel_id=456,
        source_dict=source,
    )

    assert result is True
    adapter.join_voice_channel.assert_awaited_once_with(
        voice_channel,
        text_channel_id=456,
        source=source,
    )
    assert adapter._voice_text_channels[123] == 456
    assert adapter._voice_sources[123] == source
    joined.assert_called_once_with("456", guild_id=123)


@pytest.mark.asyncio
async def test_disabled_auto_join_does_not_probe_member_voice_state():
    adapter = _adapter({"voice_auto_join": False})
    adapter.get_user_voice_channel = AsyncMock()

    result = await adapter.maybe_auto_join_voice(
        guild_id=123,
        user_id="789",
        text_channel_id=456,
    )

    assert result is False
    adapter.get_user_voice_channel.assert_not_awaited()


def test_runner_voice_join_callback_enables_persisted_mode_and_auto_tts():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._voice_mode = {}
    runner._save_voice_modes = MagicMock()
    adapter = SimpleNamespace(
        _auto_tts_enabled_chats=set(),
        _auto_tts_disabled_chats={"456"},
        _voice_input_callback=None,
    )
    runner.adapters = {Platform.DISCORD: adapter}

    runner._handle_voice_joined("456", guild_id=123)

    assert runner._voice_mode["discord:456"] == "all"
    assert adapter._auto_tts_enabled_chats == {"456"}
    assert "456" not in adapter._auto_tts_disabled_chats
    assert callable(adapter._voice_input_callback)
    runner._save_voice_modes.assert_called_once_with()
