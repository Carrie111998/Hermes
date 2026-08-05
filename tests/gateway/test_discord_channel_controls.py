"""Tests for Discord ignored_channels and no_thread_channels config."""

from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import build_session_key


def _ensure_discord_mock():
    """Install a mock discord module when discord.py isn't available."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeDMChannel:
    def __init__(self, channel_id: int = 1, name: str = "dm"):
        self.id = channel_id
        self.name = name


class FakeTextChannel:
    def __init__(self, channel_id: int = 1, name: str = "general", guild_name: str = "Hermes Server"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(name=guild_name)
        self.topic = None


class FakeThread:
    def __init__(self, channel_id: int = 1, name: str = "thread", parent=None, guild_name: str = "Hermes Server"):
        self.id = channel_id
        self.name = name
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.guild = getattr(parent, "guild", None) or SimpleNamespace(name=guild_name)
        self.topic = None


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._text_batch_delay_seconds = 0  # disable batching for tests
    adapter.handle_message = AsyncMock()
    return adapter


def make_message(*, channel, content: str, mentions=None):
    author = SimpleNamespace(id=42, display_name="TestUser", name="TestUser")
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=list(mentions or []),
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=author,
    )


# ── ignored_channels ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ignored_channel_blocks_even_with_mention(adapter, monkeypatch):
    """Ignored channels take priority — even @mentions are dropped."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "500")

    bot_user = adapter._client.user
    message = make_message(
        channel=FakeTextChannel(channel_id=500),
        content=f"<@{bot_user.id}> hello",
        mentions=[bot_user],
    )
    await adapter._handle_message(message)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_ignored_channel_processes_normally(adapter, monkeypatch):
    """Channels not in the ignored list process normally."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "500,600")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    # Stub auto-thread creation so this test focuses on ignored-channel
    # routing only.
    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=700), content="hello")
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ignored_channels_empty_string_ignores_nothing(adapter, monkeypatch):
    """Empty DISCORD_IGNORED_CHANNELS means nothing is ignored."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "")
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    # Stub auto-thread creation so this test focuses on ignored-channel
    # routing only.
    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=500), content="hello")
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()


# ── no_thread_channels ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_thread_channel_skips_auto_thread(adapter, monkeypatch):
    """Channels in no_thread_channels should not auto-create threads."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_NO_THREAD_CHANNELS", "800")
    monkeypatch.delenv("DISCORD_AUTO_THREAD", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=FakeThread(channel_id=999))

    message = make_message(channel=FakeTextChannel(channel_id=800), content="hello")
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"
    assert event.source.prospective_thread_id is None


# ── auto-thread failure policy (#20243) ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", [None, "error", "unexpected"])
async def test_auto_thread_failure_defaults_safely_to_error(
    adapter, monkeypatch, failure_mode
):
    """Default, explicit error, and unknown modes all fail closed."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    if failure_mode is None:
        monkeypatch.delenv("DISCORD_AUTO_THREAD_FAILURE_MODE", raising=False)
    else:
        monkeypatch.setenv("DISCORD_AUTO_THREAD_FAILURE_MODE", failure_mode)
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=None)

    channel = FakeTextChannel(channel_id=800)
    channel.send = AsyncMock()
    message = make_message(channel=channel, content="hello")

    handled = await adapter._handle_message(message)

    assert handled is False
    adapter._auto_create_thread.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()
    channel.send.assert_awaited_once()
    assert "request was not processed" in channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_auto_thread_failure_inline_dispatches_parent_with_prospective_scope(
    adapter, monkeypatch
):
    """Inline mode dispatches once in the parent channel,
    with the triggering message id used only as a session discriminator."""
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.setenv("DISCORD_AUTO_THREAD_FAILURE_MODE", "inline")
    monkeypatch.delenv("DISCORD_NO_THREAD_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    adapter._auto_create_thread = AsyncMock(return_value=None)

    channel = FakeTextChannel(channel_id=800)
    channel.send = AsyncMock()
    message = make_message(channel=channel, content="hello")
    await adapter._handle_message(message)

    adapter._auto_create_thread.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    channel.send.assert_not_awaited()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == "800"
    assert event.source.chat_type == "group"
    assert event.source.thread_id is None
    assert event.source.parent_chat_id is None
    assert event.source.prospective_thread_id == "123"
    ordinary_parent = SimpleNamespace(
        **{
            **event.source.__dict__,
            "prospective_thread_id": None,
        }
    )
    assert build_session_key(event.source) != build_session_key(ordinary_parent)


# ── config.py bridging ───────────────────────────────────────────────


def test_config_bridges_ignored_channels(monkeypatch, tmp_path):
    """gateway/config.py bridges discord.ignored_channels to env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "discord": {
            "ignored_channels": ["111", "222"],
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use setenv (not delenv) so monkeypatch registers cleanup even when
    # the var doesn't exist yet — load_gateway_config will overwrite it.
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("DISCORD_IGNORED_CHANNELS") == "111,222"


def test_config_bridges_auto_thread_failure_mode(monkeypatch, tmp_path):
    """discord.auto_thread_failure_mode bridges to its environment variable."""
    import yaml

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump({"discord": {"auto_thread_failure_mode": "inline"}})
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DISCORD_AUTO_THREAD_FAILURE_MODE", "")

    from gateway.config import load_gateway_config

    config = load_gateway_config()

    import os

    assert os.getenv("DISCORD_AUTO_THREAD_FAILURE_MODE") == "inline"
    assert (
        config.platforms[Platform.DISCORD].extra["auto_thread_failure_mode"]
        == "inline"
    )
