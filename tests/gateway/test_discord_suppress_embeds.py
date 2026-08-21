"""Tests for Discord link-preview suppression (``disable_link_previews``).

Discord expands URLs in a bot message into preview cards.  Masked links
(``[text](url)``) in ``content`` do **not** reliably suppress that — the only
dependable lever is the SUPPRESS_EMBEDS message flag, which has to be set per
message at send time.

Telegram already exposes ``platforms.telegram.extra.disable_link_previews``.
These tests cover the Discord counterpart, including the ``"scheduled"`` mode
that suppresses only cron deliveries (which carry ``job_id`` in their route
metadata) so interactive chat keeps its previews.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _make_adapter(disable_link_previews=None):
    extra = {} if disable_link_previews is None else {
        "disable_link_previews": disable_link_previews
    }
    return DiscordAdapter(PlatformConfig(enabled=True, token="***", extra=extra))


# --------------------------------------------------------------------------
# mode coercion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "never"),
        (False, "never"),
        ("false", "never"),
        ("off", "never"),
        ("never", "never"),
        (True, "always"),
        ("true", "always"),
        ("YES", "always"),
        ("always", "always"),
        ("scheduled", "scheduled"),
        ("  Scheduled  ", "scheduled"),
        ("cron", "scheduled"),
        ("nonsense", "never"),
    ],
)
def test_mode_coercion(raw, expected):
    assert DiscordAdapter._coerce_link_preview_mode(raw) == expected


# --------------------------------------------------------------------------
# suppression decision
# --------------------------------------------------------------------------


def test_default_keeps_previews():
    adapter = _make_adapter()
    assert adapter._should_suppress_embeds(None) is False
    assert adapter._should_suppress_embeds({"job_id": "abc123"}) is False


def test_always_suppresses_every_message():
    adapter = _make_adapter(True)
    assert adapter._should_suppress_embeds(None) is True
    assert adapter._should_suppress_embeds({}) is True
    assert adapter._should_suppress_embeds({"job_id": "abc123"}) is True


def test_scheduled_suppresses_only_cron_deliveries():
    adapter = _make_adapter("scheduled")
    # cron/scheduler.py sets route_metadata = {"job_id": job["id"]}
    assert adapter._should_suppress_embeds({"job_id": "abc123"}) is True
    # interactive chat carries no job_id and keeps its previews
    assert adapter._should_suppress_embeds(None) is False
    assert adapter._should_suppress_embeds({}) is False
    assert adapter._should_suppress_embeds({"thread_id": "42"}) is False


@pytest.mark.parametrize("configured", [None, True, "scheduled"])
def test_explicit_metadata_overrides_config(configured):
    adapter = _make_adapter(configured)
    assert adapter._should_suppress_embeds({"suppress_embeds": True}) is True
    assert adapter._should_suppress_embeds(
        {"suppress_embeds": False, "job_id": "abc123"}
    ) is False


# --------------------------------------------------------------------------
# the flag actually reaches channel.send()
# --------------------------------------------------------------------------


def _wire_channel(adapter):
    sent = MagicMock()
    sent.id = 987654321
    channel = MagicMock()
    channel.send = AsyncMock(return_value=sent)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=channel)
    adapter._client = client
    return channel


@pytest.mark.asyncio
async def test_send_passes_suppress_embeds_for_scheduled_delivery():
    adapter = _make_adapter("scheduled")
    channel = _wire_channel(adapter)

    await adapter.send("123", "see <https://example.com>", metadata={"job_id": "abc123"})

    assert channel.send.await_count >= 1
    assert channel.send.await_args.kwargs["suppress_embeds"] is True


@pytest.mark.asyncio
async def test_send_keeps_previews_for_interactive_message():
    adapter = _make_adapter("scheduled")
    channel = _wire_channel(adapter)

    await adapter.send("123", "see https://example.com")

    assert channel.send.await_count >= 1
    # The kwarg is omitted entirely rather than passed as False, so existing
    # callers and test doubles keep working unchanged.
    assert "suppress_embeds" not in channel.send.await_args.kwargs
