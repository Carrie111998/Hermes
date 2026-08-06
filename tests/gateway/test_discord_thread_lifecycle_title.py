import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


# The Discord adapter is importable in test environments without discord.py.
if "discord" not in sys.modules:
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *a, **k: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1, primary=2, secondary=2, danger=3,
        green=1, grey=2, blurple=2, red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1, green=lambda: 2, blue=lambda: 3,
        red=lambda: 4, purple=lambda: 5,
    )
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
    sys.modules["discord"] = discord_mod
    sys.modules["discord.ext"] = ext_mod
    sys.modules["discord.ext.commands"] = commands_mod

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


@pytest.mark.asyncio
async def test_rename_thread_allows_hermes_lifecycle_title_transition(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    thread = SimpleNamespace(
        name="⏳ Working · investigate auth",
        edit=AsyncMock(),
    )
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=thread),
        fetch_channel=AsyncMock(),
    )

    renamed = await adapter.rename_thread(
        "123",
        "✅ Done · investigate auth",
        only_if_current_name="raw user prompt",
        allow_current_name_prefixes=("⏳ Working · ", "✅ Done · ", "❌ Failed · "),
    )

    assert renamed is True
    thread.edit.assert_awaited_once_with(
        name="✅ Done · investigate auth",
        reason="Hermes semantic session title",
    )


@pytest.mark.asyncio
async def test_rename_thread_changes_only_lifecycle_emoji():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    thread = SimpleNamespace(
        name="thread-title",
        edit=AsyncMock(),
    )
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=thread),
        fetch_channel=AsyncMock(),
    )

    renamed = await adapter.rename_thread(
        "123",
        "",
        lifecycle_emoji="⏳",
    )

    assert renamed is True
    thread.edit.assert_awaited_once_with(
        name="⏳ thread-title",
        reason="Hermes semantic session title",
    )


@pytest.mark.asyncio
async def test_rename_thread_still_protects_human_renamed_thread(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    thread = SimpleNamespace(
        name="Ofek's pinned topic",
        edit=AsyncMock(),
    )
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=thread),
        fetch_channel=AsyncMock(),
    )

    renamed = await adapter.rename_thread(
        "123",
        "✅ Done · investigate auth",
        only_if_current_name="raw user prompt",
        allow_current_name_prefixes=("⏳ Working · ", "✅ Done · ", "❌ Failed · "),
    )

    assert renamed is False
    thread.edit.assert_not_awaited()
