from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord import adapter as discord_adapter_module
from plugins.platforms.discord.adapter import DiscordAdapter


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        files = kwargs.get("files") or []
        if files:
            file_pointer = files[0].fp
            position = file_pointer.tell()
            file_pointer.seek(0)
            sent["attached_bytes"] = file_pointer.read()
            file_pointer.seek(position)
        return SimpleNamespace(id=1234, attachments=[SimpleNamespace(id=1)])

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_prompt_uses_visible_content_with_command_and_reason():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    command = "python scripts/deploy.py --env prod --force"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=command,
        session_key="discord:555",
        description="script execution via -c flag",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None

    prompt_text = sent["content"]
    assert "Command Approval Required" in prompt_text
    assert "Do you want Hermes to run this command?" in prompt_text
    assert "Requested command" in prompt_text
    assert command in prompt_text
    assert "Reason" in prompt_text
    assert "script execution via -c flag" in prompt_text


@pytest.mark.asyncio
async def test_exact_exec_approval_attaches_every_utf8_byte_without_chunking(monkeypatch):
    class CapturedDiscordFile:
        def __init__(self, fp, filename):
            self.fp = fp
            self.filename = filename

    monkeypatch.setattr(discord_adapter_module.discord, "File", CapturedDiscordFile)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)
    approval_id = "a" * 32
    command = ("A" * 1900) + (" " * 10) + "```\n" + ("B" * 500)
    digest = __import__("hashlib").sha256(command.encode("utf-8")).hexdigest()

    result = await adapter.send_exact_exec_approval(
        chat_id="555",
        command=command,
        approval_id=approval_id,
        command_sha256=digest,
    )

    assert result.success is True
    assert len(sent["files"]) == 1
    assert sent["attached_bytes"] == command.encode("utf-8")
    assert command not in sent["content"]
    assert digest in sent["content"]
    assert f"/approve {approval_id}" in sent["content"]
