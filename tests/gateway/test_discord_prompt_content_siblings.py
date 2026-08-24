"""Sibling coverage for the embed-invisibility fix (send_exec_approval got it
in the same PR): slash confirm, clarify, and update prompts must also mirror
their payload into plain message content, since embeds don't render on some
Discord clients (web/mobile)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, _redact_discord_error_text


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_slash_confirm_mirrors_message_into_content():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_slash_confirm(
        chat_id="555",
        title="Reset session?",
        message="This will clear the current conversation history.",
        session_key="discord:555",
        confirm_id="c1",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None
    assert "Reset session?" in sent["content"]
    assert "clear the current conversation history" in sent["content"]


@pytest.mark.asyncio
async def test_clarify_with_choices_mirrors_question_into_content():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_clarify(
        chat_id="555",
        question="Which environment should I deploy to?",
        choices=["staging", "production"],
        clarify_id="cl1",
        session_key="discord:555",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert "Hermes needs your input" in sent["content"]
    assert "Which environment should I deploy to?" in sent["content"]
    assert "Pick one below" in sent["content"]


@pytest.mark.asyncio
async def test_update_and_clarify_return_redacted_transport_errors():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    secret = "synthetic-" + "discord-transport-" + "secret-1234567890"
    channel = SimpleNamespace(
        send=AsyncMock(
            side_effect=RuntimeError(
                f"transport Authorization: Bearer {secret}"
            )
        ),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    update_result = await adapter.send_update_prompt(
        chat_id="555",
        prompt="Continue update?",
        session_key="discord:555",
        prompt_id="p1",
        correlation_id="c1",
    )
    clarify_result = await adapter.send_clarify(
        chat_id="555",
        question="Which environment?",
        choices=["staging"],
        clarify_id="cl1",
        session_key="discord:555",
    )

    assert update_result.error and secret not in update_result.error
    assert "..." in update_result.error
    assert clarify_result.error and secret not in clarify_result.error
    assert "..." in clarify_result.error


@pytest.mark.asyncio
async def test_send_and_forum_transport_errors_use_redaction_boundary():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    secret = "synthetic-" + "discord-transport-" + "secret-1234567890"
    channel = SimpleNamespace(
        send=AsyncMock(
            side_effect=RuntimeError(
                f"transport Authorization: Bearer {secret}"
            )
        ),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send("555", "hello")

    assert result.error and secret not in result.error
    assert "..." in result.error

    forum = SimpleNamespace(
        id=777,
        create_thread=AsyncMock(
            side_effect=RuntimeError(f"forum Authorization: Bearer {secret}")
        ),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: forum,
        fetch_channel=AsyncMock(),
    )
    adapter._is_forum_parent = lambda _channel: True

    result = await adapter.send("555", "hello")

    assert result.error and secret not in result.error
    assert "..." in result.error
    assert result.error.startswith("Forum thread creation failed:")


def test_discord_error_redaction_masks_real_transport_secret():
    secret = "synthetic-" + "discord-transport-" + "secret-1234567890"

    redacted = _redact_discord_error_text(
        f"Discord transport failed: Authorization: Bearer {secret}"
    )

    assert secret not in redacted
    assert "..." in redacted


def test_discord_error_redaction_contains_hostile_stringification():
    class HostileError:
        def __str__(self):
            raise RuntimeError("stringification failed")

    assert _redact_discord_error_text(HostileError()) == "<discord error redacted>"
