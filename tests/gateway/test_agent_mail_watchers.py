from gateway.agent_mail_watchers import _wake_text


def test_agent_mail_wake_is_internal_and_bounded():
    text = _wake_text(
        "SilverLens",
        {"id": 42, "sender": "IronPaw", "subject": "test", "importance": "normal", "thread_id": None, "body_md": "x" * 7000},
    )
    assert text.startswith("[INTERNAL AGENT MAIL WAKE")
    assert "SilverLens" in text
    assert "message 42" in text
    assert len(text) < 6800
