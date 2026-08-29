"""DISCORD_DISABLE_DMS must drop DMs without touching guild mentions."""

from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
import plugins.platforms.discord.adapter as discord_platform


def _adapter(**extra):
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token", extra=extra))


def _wire(adapter, message):
    bot_user = SimpleNamespace(id=1517538843320914151, bot=True)
    adapter._client = SimpleNamespace(user=bot_user)
    adapter._dedup = SimpleNamespace(
        is_duplicate=lambda _id: False,
        contains=lambda _id: False,
    )
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._self_is_explicitly_mentioned = lambda _message: True
    mt = SimpleNamespace(default="default", reply="reply")
    discord_platform.discord.MessageType = mt
    message.type = mt.default
    return message


def test_disable_dms_helper_reads_extra_and_env(monkeypatch):
    assert _adapter(disable_dms=True)._discord_disable_dms() is True

    adapter = _adapter()
    adapter._gate_env_snapshot = {"DISCORD_DISABLE_DMS": "true"}
    assert adapter._discord_disable_dms() is True

    adapter = _adapter()
    adapter._gate_env_snapshot = {"DISCORD_DISABLE_DMS": ""}
    monkeypatch.delenv("DISCORD_DISABLE_DMS", raising=False)
    assert adapter._discord_disable_dms() is False


def test_admission_drops_dm_when_disabled():
    adapter = _adapter(disable_dms=True)
    message = SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=1, bot=False, name="user"),
        channel=SimpleNamespace(id=99),
        guild=None,
        mentions=[],
        content="hello",
    )
    _wire(adapter, message)
    admitted, _ = adapter._discord_message_admission(message, claim=True)
    assert admitted is False


def test_admission_keeps_guild_mention_when_dms_disabled():
    adapter = _adapter(disable_dms=True, allow_all_users="true")
    bot_user = SimpleNamespace(id=1517538843320914151, bot=True)
    message = SimpleNamespace(
        id=456,
        author=SimpleNamespace(id=2, bot=False, name="goran"),
        channel=SimpleNamespace(id=1517745765328617633),
        guild=SimpleNamespace(id=1494207325244883006),
        mentions=[bot_user],
        content="<@1517538843320914151> hello",
    )
    _wire(adapter, message)
    admitted, _ = adapter._discord_message_admission(message, claim=True)
    assert admitted is True
