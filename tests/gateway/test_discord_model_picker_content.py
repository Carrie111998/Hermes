"""Regression test for send_model_picker outbound content.

The model picker must mirror its payload into plain message content so that
web/mobile Discord clients (where embeds are invisible) can still display
the picker context.  This mirrors the pattern established by the sibling
tests in test_discord_prompt_content_siblings.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


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
async def test_model_picker_sends_content_embed_and_view():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_model_picker(
        chat_id="555",
        providers=[
            {
                "slug": "openrouter",
                "name": "OpenRouter",
                "models": ["openai/gpt-5-mini"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="openai/gpt-5-mini",
        current_provider="openrouter",
        session_key="discord:555",
        on_model_selected=AsyncMock(return_value="ok"),
    )

    assert result.success is True
    # All three pieces must be present — content for web/mobile, embed for desktop
    assert sent["view"] is not None
    assert sent["embed"] is not None
    assert sent.get("content") is not None


@pytest.mark.asyncio
async def test_model_picker_content_includes_model_and_provider():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_model_picker(
        chat_id="555",
        providers=[
            {
                "slug": "copilot",
                "name": "GitHub Copilot",
                "models": ["gpt-5.4"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model="gpt-5.4",
        current_provider="copilot",
        session_key="discord:555",
        on_model_selected=AsyncMock(return_value="ok"),
    )

    assert result.success is True
    content = sent["content"]
    # Content must include model/provider selection context
    assert "Select a provider" in content
    assert "gpt-5.4" in content
    assert "copilot" in content.lower()


@pytest.mark.asyncio
async def test_model_picker_content_falls_back_for_unknown_model():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_model_picker(
        chat_id="555",
        providers=[
            {
                "slug": "openrouter",
                "name": "OpenRouter",
                "models": ["deepseek/deepseek-r1"],
                "total_models": 1,
                "is_current": True,
            }
        ],
        current_model=None,
        current_provider="openrouter",
        session_key="discord:555",
        on_model_selected=AsyncMock(return_value="ok"),
    )

    assert result.success is True
    content = sent["content"]
    assert "unknown" in content
    assert "openrouter" in content.lower()
