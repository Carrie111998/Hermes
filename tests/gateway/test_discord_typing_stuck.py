"""Discord typing-loop races: closed-gate, finally-guard, bounded stop.

Tracks #85427 (class) / #85425 (orphan pop) and the late progress-path
send_typing recreate we hit on Hermes 0.20.x after ✅.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.http = SimpleNamespace(Route=MagicMock)
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _adapter() -> DiscordAdapter:
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = MagicMock()
    adapter._client.http = MagicMock()
    adapter._client.http.request = AsyncMock()
    adapter._typing_tasks = {}
    return adapter


@pytest.mark.asyncio
async def test_send_typing_after_stop_does_not_recreate_loop():
    """Late progress send_typing must not start a new loop after stop_typing."""
    adapter = _adapter()
    await adapter.send_typing("chan")
    assert "chan" in adapter._typing_tasks
    await adapter.stop_typing("chan")
    assert "chan" not in adapter._typing_tasks
    assert "chan" in adapter._typing_closed

    await adapter.send_typing("chan")
    assert "chan" not in adapter._typing_tasks


@pytest.mark.asyncio
async def test_keep_typing_reopens_closed_gate():
    adapter = _adapter()
    await adapter.stop_typing("chan")
    assert "chan" in adapter._typing_closed
    adapter._typing_closed.discard("chan")
    await adapter.send_typing("chan")
    assert "chan" in adapter._typing_tasks
    await adapter.stop_typing("chan")


@pytest.mark.asyncio
async def test_typing_finally_does_not_orphan_newer_loop():
    """Stale loop finally must not pop a replacement registered for the same chat."""
    adapter = _adapter()
    await adapter.send_typing("chan")
    loop_a = adapter._typing_tasks["chan"]
    adapter._typing_tasks.pop("chan")
    adapter._typing_closed.discard("chan")
    await adapter.send_typing("chan")
    loop_b = adapter._typing_tasks["chan"]
    assert loop_b is not loop_a
    loop_a.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(loop_a), timeout=0.5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    assert adapter._typing_tasks.get("chan") is loop_b
    await adapter.stop_typing("chan")
