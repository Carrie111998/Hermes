from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from tools import discord_approval_box_tool as approval_tool


def test_first_resolution_wins_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_tool, "get_hermes_home", lambda: Path(tmp_path))

    record = approval_tool.create_approval_record(
        title="Email draft",
        body="Draft for client review.",
        drive_url="https://drive.google.com/file/d/example/view",
        channel_id="123",
    )
    resolved = approval_tool.resolve_approval(record["id"], "approved", "Willie")

    assert resolved is not None
    assert resolved["status"] == "approved"
    assert resolved["resolved_by"] == "Willie"
    assert approval_tool.resolve_approval(record["id"], "rejected", "Other") is None
    assert approval_tool.get_approval_record(record["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_discord_deliverable_approval_has_exactly_three_review_controls():
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send_deliverable_approval(
        chat_id="555",
        title="Ron article-email draft",
        body="Prepared for review before release.",
        drive_url="https://drive.google.com/file/d/example/view",
        approval_id="abc123",
    )

    assert result.success is True
    assert "Approve" in sent["content"]
    assert "Needs Work" in sent["content"]
    assert "Reject" in sent["content"]
    assert sent["view"] is not None
