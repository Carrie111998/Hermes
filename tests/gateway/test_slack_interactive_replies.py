"""Behavior contracts for Slack interactive reply directives and storage."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.interactive_replies import (
    InteractiveButton,
    InteractiveReplyStore,
    append_actions_block,
    parse_interactive_reply,
)


def _make_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.222"})
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    return adapter, client


def test_parse_valid_directive_strips_it_and_preserves_visible_reply():
    reply = parse_interactive_reply(
        "Approved lead.\n[[slack_buttons: Enroll:enroll, Skip:skip]]"
    )

    assert reply is not None
    assert reply.visible_content == "Approved lead."
    assert [button.action_id for button in reply.buttons] == ["enroll", "skip"]


def test_parse_multiple_valid_trailing_directives_in_original_order():
    reply = parse_interactive_reply(
        "Approved lead.\n"
        "[[slack_buttons: Enroll:enroll]]\n"
        "[[slack_buttons: Skip:skip, Draft:draft]]"
    )

    assert reply is not None
    assert reply.visible_content == "Approved lead."
    assert [button.action_id for button in reply.buttons] == [
        "enroll",
        "skip",
        "draft",
    ]


def test_parse_malformed_directives_return_none_for_literal_fallback():
    invalid = (
        "[[slack_buttons: :enroll]]",
        "[[slack_buttons: Enroll:]]",
        "[[slack_buttons: Enroll:enroll, Again:enroll]]",
        "[[slack_buttons: Enroll:not valid]]",
        "[[slack_buttons: Enroll:enroll]]\ntrailing text",
        "[[slack_buttons: " + ", ".join(f"B{i}:a{i}" for i in range(26)) + "]]",
    )

    for content in invalid:
        assert parse_interactive_reply(content) is None


def test_consume_requires_bound_message_and_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    store = InteractiveReplyStore(ttl_seconds=60)
    prepared = store.create_card("C1", "T1", (InteractiveButton("Enroll", "enroll"),))

    assert store.consume(prepared.buttons[0].token, "C1", "M1") is None
    assert store.bind_message(prepared.card_id, "M1") is True
    consumed = store.consume(prepared.buttons[0].token, "C1", "M1")
    assert consumed is not None
    assert consumed.action_id == "enroll"
    assert store.consume(prepared.buttons[0].token, "C1", "M1") is None


def test_consume_rejects_expired_and_cross_channel_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    store = InteractiveReplyStore(ttl_seconds=60)
    prepared = store.create_card("C1", None, (InteractiveButton("Skip", "skip"),))
    assert store.bind_message(prepared.card_id, "M1") is True
    assert store.consume(prepared.buttons[0].token, "C2", "M1") is None
    assert store.consume(prepared.buttons[0].token, "C1", "M2") is None

    expired_store = InteractiveReplyStore(ttl_seconds=-1)
    expired = expired_store.create_card("C1", None, (InteractiveButton("Skip", "skip"),))
    assert expired_store.bind_message(expired.card_id, "M2") is False
    assert expired_store.consume(expired.buttons[0].token, "C1", "M2") is None


def test_append_actions_block_returns_slack_buttons_without_mutating_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    store = InteractiveReplyStore()
    prepared = store.create_card(
        "C1",
        None,
        (InteractiveButton("Enroll", "enroll"), InteractiveButton("Skip", "skip")),
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Lead"}}]

    rendered = append_actions_block(blocks, prepared)

    assert rendered[:-1] == blocks
    assert rendered is not blocks
    assert rendered[-1]["type"] == "actions"
    assert [item["action_id"] for item in rendered[-1]["elements"]] == [
        "hermes_interactive_reply",
        "hermes_interactive_reply",
    ]
    assert [item["value"] for item in rendered[-1]["elements"]] == [
        button.token for button in prepared.buttons
    ]


@pytest.mark.asyncio
async def test_adapter_send_posts_buttons_without_literal_directive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()

    await adapter.send("C1", "Lead ready\n[[slack_buttons: Enroll:enroll]]")

    posted = client.chat_postMessage.await_args.kwargs
    assert "[[slack_buttons:" not in posted["text"]
    assert posted["text"]
    assert posted["blocks"][-1]["type"] == "actions"
    assert posted["blocks"][-1]["elements"][0]["action_id"] == "hermes_interactive_reply"

@pytest.mark.asyncio
async def test_adapter_send_posts_slash_reply_buttons_ephemerally(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    client.chat_postEphemeral = AsyncMock(
        return_value={"ok": True, "message_ts": "222.333"}
    )
    adapter._pop_slash_context = MagicMock(return_value={"user_id": "U1"})
    adapter._clear_thread_status_quietly = AsyncMock()

    result = await adapter.send("C1", "Lead ready\n[[slack_buttons: Enroll:enroll]]")

    assert result.success is True
    posted = client.chat_postEphemeral.await_args.kwargs
    assert "[[slack_buttons:" not in posted["text"]
    assert posted["blocks"][-1]["type"] == "actions"
    assert posted["blocks"][-1]["elements"][0]["action_id"] == "hermes_interactive_reply"


@pytest.mark.asyncio
async def test_adapter_send_posts_one_button_control_after_long_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    content = "x" * (adapter.MAX_MESSAGE_LENGTH + 1)

    await adapter.send("C1", content + "\n[[slack_buttons: Enroll:enroll]]")

    assert client.chat_postMessage.await_count >= 2
    control = client.chat_postMessage.await_args.kwargs
    assert control["text"] == "Interactive reply options"
    assert control["blocks"][-1]["type"] == "actions"
    assert control["blocks"][-1]["elements"][0]["action_id"] == "hermes_interactive_reply"
