"""Behavior contracts for Slack interactive reply directives and storage."""

from plugins.platforms.slack.interactive_replies import (
    InteractiveButton,
    InteractiveReplyStore,
    append_actions_block,
    parse_interactive_reply,
)


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
