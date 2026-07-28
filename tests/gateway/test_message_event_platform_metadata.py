from gateway.platforms.base import MessageEvent, Platform, SessionSource


def test_message_event_builds_chat_scoped_reply_metadata():
    event = MessageEvent(
        text="ack",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="42",
            chat_id="8531920232",
            chat_type="dm",
        ),
        message_id="4456",
        reply_to_message_id="4455",
    )

    assert event.platform_persistence_metadata() == {
        "platform": "telegram",
        "chat_id": "8531920232",
        "message_id": "4456",
        "reply_to_message_id": "4455",
    }