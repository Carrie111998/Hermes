"""Behavior contracts for Slack's adaptive channel/thread reply mode."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import (
    SlackAdapter,
    _normalize_slack_reply_mode,
    _slack_auto_reply_in_thread,
)


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="xoxb-fake-token")
    config.extra["reply_in_thread"] = "auto"
    instance = SlackAdapter(config)
    instance._app = MagicMock()
    instance._app.client = AsyncMock()
    instance._bot_user_id = "U_BOT"
    instance._running = True
    instance.handle_message = AsyncMock()
    return instance


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )


def _channel_event(
    text: str,
    *,
    ts: str = "1700000000.000001",
    thread_ts: str | None = None,
    files: list[dict] | None = None,
) -> dict:
    event = {
        "channel": "C_CHAN",
        "channel_type": "channel",
        "user": "U_USER",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if files is not None:
        event["files"] = files
    return event


async def _capture_event(adapter: SlackAdapter, event: dict):
    captured = []
    adapter.handle_message = AsyncMock(side_effect=captured.append)
    with patch.object(
        adapter,
        "_resolve_user_name",
        new=AsyncMock(return_value="testuser"),
    ):
        await adapter._handle_slack_message(event)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, "thread"),
        (False, "channel"),
        ("true", "thread"),
        ("false", "channel"),
        ("thread", "thread"),
        ("channel", "channel"),
        ("auto", "auto"),
        ("unexpected", "thread"),
        (None, "thread"),
    ],
)
def test_reply_mode_normalizes_legacy_and_adaptive_values(raw, expected):
    assert _normalize_slack_reply_mode(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Thanks — that makes sense.",
        "What do you think?",
        "Can you give me the short version?",
    ],
)
def test_auto_policy_keeps_compact_conversation_in_channel(text):
    assert _slack_auto_reply_in_thread(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Please investigate why the deployment is failing.",
        "Walk me through the tradeoffs step-by-step.",
        "Please handle these:\n- check the logs\n- compare the failures",
        "Can you explain this code?\n```python\nraise RuntimeError('boom')\n```",
    ],
)
def test_auto_policy_threads_clearly_involved_requests(text):
    assert _slack_auto_reply_in_thread(text) is True


def test_explicit_channel_direction_overrides_involved_request():
    assert (
        _slack_auto_reply_in_thread(
            "Please investigate the failure, but keep this in the channel."
        )
        is False
    )


@pytest.mark.parametrize(
    "text",
    [
        "Please keep it in the channel.",
        "Stay here.",
        "Don't use a thread.",
    ],
)
def test_common_channel_directions_stay_in_channel(text):
    assert _slack_auto_reply_in_thread(text) is False


def test_explicit_thread_direction_overrides_short_message():
    assert _slack_auto_reply_in_thread("Quick thought—thread this.") is True


def test_gateway_config_preserves_adaptive_mode(monkeypatch, tmp_path):
    from gateway.config import Platform, load_gateway_config

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "slack:\n"
        "  reply_in_thread: auto\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    config = load_gateway_config()

    assert config.platforms[Platform.SLACK].extra["reply_in_thread"] == "auto"


def test_short_top_level_turn_uses_channel_session(adapter):
    message = asyncio.run(
        _capture_event(
            adapter,
            _channel_event("<@U_BOT> What do you think?"),
        )
    )

    assert message.source.thread_id is None
    assert message.reply_to_message_id is None
    assert (
        adapter._resolve_thread_ts(
            reply_to=message.message_id,
            metadata={"slack_team_id": "T_TEAM"},
        )
        is None
    )


def test_involved_top_level_turn_uses_thread_session(adapter):
    message = asyncio.run(
        _capture_event(
            adapter,
            _channel_event("<@U_BOT> Please investigate the deployment failure."),
        )
    )

    assert message.source.thread_id == message.message_id
    assert (
        adapter._resolve_thread_ts(
            reply_to=message.message_id,
            metadata={
                "thread_id": message.source.thread_id,
                "slack_team_id": "T_TEAM",
            },
        )
        == message.message_id
    )


def test_existing_thread_always_stays_threaded(adapter):
    message = asyncio.run(
        _capture_event(
            adapter,
            _channel_event(
                "<@U_BOT> Thanks.",
                ts="1700000000.000002",
                thread_ts="1700000000.000001",
            ),
        )
    )

    assert message.source.thread_id == "1700000000.000001"
    assert message.reply_to_message_id == "1700000000.000001"


def test_multiple_files_are_thread_worthy(adapter):
    message = asyncio.run(
        _capture_event(
            adapter,
            _channel_event(
                "<@U_BOT> Thoughts?",
                files=[
                    {"id": "F_ONE", "name": "one.png"},
                    {"id": "F_TWO", "name": "two.png"},
                ],
            ),
        )
    )

    assert message.source.thread_id == message.message_id
