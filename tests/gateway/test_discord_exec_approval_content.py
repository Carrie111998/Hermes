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
async def test_protected_instruction_prompt_shows_path_diff_and_one_time_button():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    result = await adapter.send_exec_approval(
        chat_id="555",
        command="<write to AGENTS.md>",
        session_key="discord:555",
        description="Protected instruction files require approval.",
        metadata={
            "protected_paths": ["/work/project/AGENTS.md"],
            "proposed_diff": "--- /dev/null\n+++ /work/project/AGENTS.md\n+allow one task\n",
        },
        allow_permanent=False,
        allow_session=False,
    )

    assert result.success is True
    assert "Protected instruction-file approval required" in sent["content"]
    assert "/work/project/AGENTS.md" in sent["content"]
    assert "+allow one task" in sent["content"]
    labels = {getattr(child, "label", "") for child in sent["view"].children}
    assert "Approve once" in labels
    assert "Deny" in labels
    assert "Allow Session" not in labels
    assert "Always Allow" not in labels


