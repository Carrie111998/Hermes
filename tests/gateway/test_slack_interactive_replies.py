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
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.users_info = AsyncMock(
        return_value={
            "ok": True,
            "user": {
                "id": "U1",
                "name": "alice",
                "profile": {"display_name": "Alice"},
            },
        }
    )
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"id": "C1", "name": "leads"}}
    )
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    return adapter, client


def _click_body(
    *,
    channel: str = "C1",
    message_ts: str = "M1",
    thread_ts: str | None = "T1",
    user: str = "U1",
) -> dict:
    message = {
        "ts": message_ts,
        "text": "Lead ready",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Lead ready"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "hermes_interactive_reply",
                        "value": "opaque",
                    }
                ],
            },
        ],
    }
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return {
        "team": {"id": "W1"},
        "channel": {"id": channel, "name": "leads"},
        "user": {"id": user, "name": "alice"},
        "message": message,
    }


class _StringifiesTo:
    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:
        return self._value


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

@pytest.mark.asyncio
async def test_adapter_send_fails_when_long_reply_control_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    client.chat_postMessage = AsyncMock(
        side_effect=[
            {"ts": "111.001"},
            {"ts": "111.002"},
            {"ok": False, "error": "invalid_blocks"},
        ]
    )
    content = "x" * (adapter.MAX_MESSAGE_LENGTH + 1)

    result = await adapter.send("C1", content + "\n[[slack_buttons: Enroll:enroll]]")

    assert result.success is False
    assert result.error == "Slack interactive control failed: invalid_blocks"

@pytest.mark.asyncio
async def test_adapter_slash_and_long_controls_are_single_and_opaque(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    slash_adapter, slash_client = _make_adapter()
    slash_client.chat_postEphemeral = AsyncMock(
        return_value={"ok": True, "message_ts": "222.333"}
    )
    slash_adapter._pop_slash_context = MagicMock(return_value={"user_id": "U1"})
    slash_adapter._clear_thread_status_quietly = AsyncMock()
    await slash_adapter.send("C1", "Lead\n[[slack_buttons: Enroll:enroll]]")
    slash_actions = [
        block
        for call in slash_client.chat_postEphemeral.await_args_list
        for block in call.kwargs.get("blocks", [])
        if block["type"] == "actions"
    ]

    long_adapter, long_client = _make_adapter()
    content = "x" * (long_adapter.MAX_MESSAGE_LENGTH + 1)
    await long_adapter.send("C1", content + "\n[[slack_buttons: Enroll:enroll]]")
    long_actions = [
        block
        for call in long_client.chat_postMessage.await_args_list
        for block in call.kwargs.get("blocks", [])
        if block["type"] == "actions"
    ]

    for actions in (slash_actions, long_actions):
        assert len(actions) == 1
        assert actions[0]["elements"][0]["value"] != "enroll"


@pytest.mark.asyncio
async def test_valid_click_relays_as_user_event_in_original_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.config.extra.update(
        {
            "channel_prompts": {"C1": "Treat this channel as lead operations."},
            "channel_skill_bindings": [{"id": "C1", "skills": ["lead-ops"]}],
        }
    )
    adapter._build_identity_prompt = MagicMock(return_value="Slack identity")
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")
    body = _click_body()
    ack = AsyncMock()

    await adapter._handle_interactive_reply_action(
        ack,
        body,
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    ack.assert_awaited_once_with()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Slack button action: go"
    assert event.source.chat_id == "C1"
    assert event.source.chat_name == "leads"
    assert event.source.chat_type == "group"
    assert event.source.user_id == "U1"
    assert event.source.user_name == "Alice"
    assert event.source.thread_id == "T1"
    assert event.source.scope_id == "W1"
    assert event.message_id == "M1"
    assert event.reply_to_message_id == "T1"
    assert event.channel_prompt == (
        "Slack identity\n\nTreat this channel as lead operations."
    )
    assert event.auto_skill == ["lead-ops"]
    assert event.metadata == {
        "slack_team_id": "W1",
        "slack_channel_id": "C1",
        "slack_thread_ts": "T1",
    }
    assert event.raw_message is body
    assert event.internal is False
    update = client.chat_update.await_args.kwargs
    assert update["channel"] == "C1"
    assert update["ts"] == "M1"
    assert all(block["type"] != "actions" for block in update["blocks"])


@pytest.mark.asyncio
async def test_forged_or_replayed_click_never_reaches_handle_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, _client = _make_adapter()
    adapter.handle_message = AsyncMock()
    ack = AsyncMock()

    await adapter._handle_interactive_reply_action(
        ack, _click_body(), {"action_id": "hermes_interactive_reply", "value": "forged"}
    )
    adapter.handle_message.assert_not_awaited()

    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")
    action = {
        "action_id": "hermes_interactive_reply",
        "value": prepared.buttons[0].token,
    }
    await adapter._handle_interactive_reply_action(ack, _click_body(), action)
    await adapter._handle_interactive_reply_action(ack, _click_body(), action)

    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_cross_channel_or_wrong_action_id_click_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, _client = _make_adapter()
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(channel="C2"),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )
    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(),
        {"action_id": "untrusted_action", "value": prepared.buttons[0].token},
    )

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_card_update_does_not_reopen_or_duplicate_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.handle_message = AsyncMock()
    client.chat_update = AsyncMock(side_effect=RuntimeError("update rejected"))
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")
    action = {
        "action_id": "hermes_interactive_reply",
        "value": prepared.buttons[0].token,
    }

    await adapter._handle_interactive_reply_action(AsyncMock(), _click_body(), action)
    await adapter._handle_interactive_reply_action(AsyncMock(), _click_body(), action)

    assert adapter.handle_message.await_count == 1
    assert client.chat_update.await_count == 1


@pytest.mark.asyncio
async def test_ignored_channel_click_does_not_consume_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.config.extra["ignored_channels"] = ["C1"]
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    adapter.handle_message.assert_not_awaited()
    client.conversations_info.assert_not_awaited()
    assert (
        adapter._interactive_reply_store.consume(
            prepared.buttons[0].token, "C1", "M1"
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "channel_record"),
    [
        ("D1", {"id": "D1", "is_im": True, "user": "U1"}),
        ("G1", {"id": "G1", "name": "mpdm-team", "is_mpim": True}),
    ],
)
async def test_disabled_dm_click_does_not_consume_card(
    tmp_path, monkeypatch, channel_id, channel_record
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.config.extra.update(
        {"disable_dms": True, "allowed_channels": [channel_id]}
    )
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": channel_record}
    )
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        channel_id, "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(channel=channel_id),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    adapter.handle_message.assert_not_awaited()
    assert (
        adapter._interactive_reply_store.consume(
            prepared.buttons[0].token, channel_id, "M1"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_non_allowed_channel_click_does_not_consume_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.config.extra["allowed_channels"] = ["C1"]
    client.conversations_info = AsyncMock(
        return_value={
            "ok": True,
            "channel": {"id": "C2", "name": "other-channel"},
        }
    )
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C2", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(channel="C2"),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    adapter.handle_message.assert_not_awaited()
    assert (
        adapter._interactive_reply_store.consume(
            prepared.buttons[0].token, "C2", "M1"
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "message_ts", "user", "token"])
async def test_non_string_required_callback_field_does_not_consume_card(
    tmp_path, monkeypatch, field
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    client.conversations_info = AsyncMock(
        return_value={
            "ok": True,
            "channel": {"id": "123", "name": "numbers"},
        }
    )
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "123", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "456")
    body = _click_body(channel="123", message_ts="456", user="789")
    action = {
        "action_id": "hermes_interactive_reply",
        "value": prepared.buttons[0].token,
    }
    if field == "channel":
        body["channel"]["id"] = 123
    elif field == "message_ts":
        body["message"]["ts"] = 456
    elif field == "user":
        body["user"]["id"] = 789
    else:
        action["value"] = _StringifiesTo(prepared.buttons[0].token)

    await adapter._handle_interactive_reply_action(AsyncMock(), body, action)

    adapter.handle_message.assert_not_awaited()
    assert (
        adapter._interactive_reply_store.consume(
            prepared.buttons[0].token, "123", "456"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_wrong_message_timestamp_does_not_consume_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, _client = _make_adapter()
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(message_ts="M2"),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    adapter.handle_message.assert_not_awaited()
    assert (
        adapter._interactive_reply_store.consume(
            prepared.buttons[0].token, "C1", "M1"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_card_cleanup_preserves_unrelated_control_in_shared_actions_block(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        "C1", "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")
    body = _click_body()
    body["message"]["blocks"][-1]["elements"].append(
        {
            "type": "button",
            "action_id": "unrelated_control",
            "value": "keep",
        }
    )

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        body,
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    updated_blocks = client.chat_update.await_args.kwargs["blocks"]
    actions = [block for block in updated_blocks if block["type"] == "actions"]
    assert len(actions) == 1
    assert [element["action_id"] for element in actions[0]["elements"]] == [
        "unrelated_control"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "channel_record", "allowed_channels"),
    [
        (
            "D1",
            {"id": "D1", "is_im": True, "user": "U1"},
            ["C_OTHER"],
        ),
        (
            "G1",
            {"id": "G1", "is_mpim": True, "name": "mpdm-team"},
            ["G1"],
        ),
    ],
)
async def test_im_and_mpim_clicks_use_dm_source_classification(
    tmp_path, monkeypatch, channel_id, channel_record, allowed_channels
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    adapter, client = _make_adapter()
    adapter.config.extra["allowed_channels"] = allowed_channels
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": channel_record}
    )
    adapter.handle_message = AsyncMock()
    prepared = adapter._interactive_reply_store.create_card(
        channel_id, "T1", (InteractiveButton("Go", "go"),)
    )
    assert adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")

    await adapter._handle_interactive_reply_action(
        AsyncMock(),
        _click_body(channel=channel_id),
        {
            "action_id": "hermes_interactive_reply",
            "value": prepared.buttons[0].token,
        },
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "dm"
