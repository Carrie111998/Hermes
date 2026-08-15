"""Slack pinned-message system prompt tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SLACK_PINNED_FILES_METADATA_KEY
from plugins.platforms.slack.adapter import SLACK_DOCUMENT_MAX_BYTES, SlackAdapter


def _adapter(*, extra=None):
    adapter = SlackAdapter(
        PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    )
    client = AsyncMock()
    client.users_info.return_value = {
        "ok": True,
        "user": {
            "is_bot": False,
            "profile": {"display_name": "Test User"},
            "real_name": "Test User",
        },
    }
    client.conversations_info.return_value = {
        "ok": True,
        "channel": {"name": "project"},
    }
    adapter._app = MagicMock()
    adapter._app.client = client
    adapter._bot_user_id = "U_BOT"
    adapter._bot_display_name = "Hermes"
    adapter._team_bot_names = {"T1": "Hermes"}
    adapter._running = True
    adapter.handle_message = AsyncMock()
    adapter.send = AsyncMock()
    return adapter, client


@pytest.mark.asyncio
async def test_fetch_pinned_context_renders_text_and_document_ids():
    adapter, client = _adapter()
    client.pins_list.return_value = {
        "ok": True,
        "items": [
            {
                "type": "message",
                "message": {
                    "text": "Use British spelling.",
                    "files": [
                        {
                            "id": "F0BQBQ4MVJR",
                            "name": "war-and-peace.txt",
                            "mimetype": "text/plain",
                        }
                    ],
                },
            },
            {"type": "file", "file": {"id": "F_IGNORED"}},
        ],
    }

    prompt, allowed_files = await adapter._fetch_pinned_channel_context("C1", "T1")

    assert "## Pinned Slack channel context" in prompt
    assert "Use British spelling." in prompt
    assert "war-and-peace.txt (Slack file ID: F0BQBQ4MVJR)" in prompt
    assert "slack_download_pinned_file" in prompt
    assert allowed_files == {"F0BQBQ4MVJR": "war-and-peace.txt"}
    client.pins_list.assert_awaited_once_with(channel="C1")
    client.files_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_lazy_download_resolves_files_info_before_downloading(monkeypatch):
    adapter, client = _adapter()
    client.files_info.return_value = {
        "ok": True,
        "file": {
            "id": "F0BQBQ4MVJR",
            "name": "war-and-peace.txt",
            "mimetype": "text/plain",
            "size": 1024,
            "url_private_download": "https://files.slack.com/war-and-peace.txt",
        },
    }
    adapter._download_slack_file_bytes = AsyncMock(return_value=b"book text")
    monkeypatch.setattr(
        "plugins.platforms.slack.adapter.cache_document_from_bytes",
        lambda data, filename: f"/tmp/{filename}",
    )

    result = await adapter.download_pinned_file(
        file_id="F0BQBQ4MVJR", channel_id="C1", team_id="T1"
    )

    client.files_info.assert_awaited_once_with(file="F0BQBQ4MVJR")
    adapter._download_slack_file_bytes.assert_awaited_once_with(
        "https://files.slack.com/war-and-peace.txt", team_id="T1"
    )
    assert result == {
        "path": "/tmp/war-and-peace.txt",
        "name": "war-and-peace.txt",
        "mimetype": "text/plain",
    }


@pytest.mark.asyncio
async def test_lazy_download_rejects_unexpected_files_info_result():
    adapter, client = _adapter()
    client.files_info.return_value = {
        "ok": True,
        "file": {
            "id": "F_OTHER",
            "name": "wrong.txt",
            "size": 10,
            "url_private_download": "https://files.slack.com/wrong.txt",
        },
    }
    adapter._download_slack_file_bytes = AsyncMock()

    with pytest.raises(RuntimeError, match="unexpected file"):
        await adapter.download_pinned_file(
            file_id="F_ALLOWED", channel_id="C1", team_id="T1"
        )

    adapter._download_slack_file_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_message_combines_pins_with_existing_channel_prompt():
    adapter, client = _adapter(
        extra={"channel_prompts": {"C1": "Configured channel prompt."}}
    )
    client.pins_list.return_value = {
        "ok": True,
        "items": [
            {
                "type": "message",
                "message": {"text": "Pinned project instruction.", "files": []},
            }
        ],
    }

    await adapter._handle_slack_message({
        "text": "<@U_BOT> hello",
        "user": "U_USER",
        "channel": "C1",
        "channel_type": "channel",
        "team": "T1",
        "ts": "1786799151.464049",
    })

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "Configured channel prompt." in event.channel_prompt
    assert "Pinned project instruction." in event.channel_prompt
    assert "@Hermes" in event.channel_prompt
    assert event.metadata[SLACK_PINNED_FILES_METADATA_KEY] == {}


@pytest.mark.asyncio
async def test_lazy_download_uses_attachment_pipeline_document_size_limit():
    adapter, client = _adapter()
    client.files_info.return_value = {
        "ok": True,
        "file": {
            "id": "F_LARGE",
            "name": "large.txt",
            "size": SLACK_DOCUMENT_MAX_BYTES + 1,
            "url_private_download": "https://files.slack.com/large.txt",
        },
    }
    adapter._download_slack_file_bytes = AsyncMock()

    with pytest.raises(ValueError, match="too large"):
        await adapter.download_pinned_file(
            file_id="F_LARGE", channel_id="C1", team_id="T1"
        )

    adapter._download_slack_file_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_pins_list_failure_posts_error_and_stops_dispatch():
    adapter, client = _adapter()
    client.pins_list.side_effect = RuntimeError("Slack unavailable")

    await adapter._handle_slack_message({
        "text": "<@U_BOT> hello",
        "user": "U_USER",
        "channel": "C1",
        "channel_type": "channel",
        "team": "T1",
        "ts": "1786799151.464049",
    })

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    assert "pinned" in adapter.send.await_args.args[1].lower()
