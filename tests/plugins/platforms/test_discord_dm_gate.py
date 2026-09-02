"""Discord DM admission is profile-scoped and can be closed to the LLM.

Knight Co handles deterministic approval commands outside the LLM conversation
path.  Its profile must therefore be able to reject every Discord DM while
continuing to admit its allowlisted guild channel.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("discord")

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord import adapter as adapter_module
from plugins.platforms.discord.adapter import DiscordAdapter, _GATE_ENV_KEYS


class _Dedup:
    def is_duplicate(self, _message_id):
        return False

    def contains(self, _message_id):
        return False


class _DMChannel:
    id = 9001


class _GuildChannel:
    id = 9002
    parent_id = None


def _adapter(extra: dict | None = None, snapshot: dict | None = None) -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="x", extra=dict(extra or {}))
    adapter._gate_env_snapshot = {
        key: (snapshot or {}).get(key, "") for key in _GATE_ENV_KEYS
    }
    adapter._allowed_user_ids = {"42"}
    adapter._allowed_role_ids = set()
    adapter._dedup = _Dedup()
    bot_user = SimpleNamespace(id=999, bot=True)
    adapter._client = SimpleNamespace(user=bot_user)
    adapter._is_allowed_user = lambda *_args, **_kwargs: True
    adapter._get_parent_channel_id = lambda _channel: None
    return adapter


def _message(channel, *, guild=None):
    return SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=42, bot=False),
        channel=channel,
        guild=guild,
        type=adapter_module.discord.MessageType.default,
        mentions=[],
        content="hello",
    )


@pytest.fixture(autouse=True)
def _dm_channel_type(monkeypatch):
    monkeypatch.setattr(adapter_module.discord, "DMChannel", _DMChannel)
    monkeypatch.delenv("DISCORD_ALLOW_DMS", raising=False)


def test_dms_are_enabled_by_default():
    adapter = _adapter()
    assert adapter._discord_dms_enabled() is True
    assert adapter._discord_message_admission(_message(_DMChannel()), claim=False) == (
        True,
        False,
    )


@pytest.mark.parametrize("value", [False, "false", "0", "no", "off", "FALSE"])
def test_profile_config_disables_llm_dm_admission(value):
    adapter = _adapter({"allow_dms": value})
    assert adapter._discord_dms_enabled() is False
    assert adapter._discord_message_admission(_message(_DMChannel()), claim=False) == (
        False,
        False,
    )


def test_profile_config_takes_precedence_over_legacy_env_snapshot():
    adapter = _adapter(
        {"allow_dms": False},
        snapshot={"DISCORD_ALLOW_DMS": "true"},
    )
    assert adapter._discord_dms_enabled() is False


def test_real_gateway_loader_preserves_discord_allow_dms(tmp_path, monkeypatch):
    """The documented config path must survive the real gateway loader."""
    from gateway import config as gateway_config

    (tmp_path / "config.yaml").write_text(
        "discord:\n  enabled: true\n  allow_dms: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_config, "get_hermes_home", lambda: tmp_path)

    loaded = gateway_config.load_gateway_config()

    assert loaded.platforms[Platform.DISCORD].extra["allow_dms"] is False


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_legacy_env_snapshot_disables_llm_dm_admission(value):
    adapter = _adapter(snapshot={"DISCORD_ALLOW_DMS": value})
    assert adapter._discord_dms_enabled() is False
    assert adapter._discord_message_admission(_message(_DMChannel()), claim=False) == (
        False,
        False,
    )


def test_dm_gate_does_not_block_guild_messages():
    adapter = _adapter({"allow_dms": False})
    guild_message = _message(_GuildChannel(), guild=SimpleNamespace(id=77))
    assert adapter._discord_message_admission(guild_message, claim=False) == (
        True,
        False,
    )
