"""Contract tests for the lightweight Zulip native tool capability."""

import json

from tools.zulip_tool import (
    _build_permalink,
    _check_zulip_available,
    _handle_get_topics,
    _handle_get_message_link,
    _handle_list_channels,
    _handle_read_topic,
    _handle_search,
    _handle_send_message,
    _normalize_messages,
    _normalize_topics,
)


def test_permalink_uses_configured_instance_and_message_id(monkeypatch):
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example.test:8666/")
    assert _build_permalink(42) == "https://zulip.example.test:8666/#narrow/near/42"


def test_normalize_topics_exposes_name_and_last_message_id():
    result = _normalize_topics({"topics": [{"name": "Integration", "max_id": 23}]})
    assert result == {"count": 1, "topics": [{"name": "Integration", "last_message_id": 23}]}


def test_normalize_messages_returns_sender_timestamp_content_and_permalink(monkeypatch):
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example.test")
    result = _normalize_messages({"messages": [{
        "id": 7,
        "sender_full_name": "Hermes Bot",
        "timestamp": 1735689600,
        "content": "Status updated",
        "subject": "Integration",
        "display_recipient": "HOG",
    }]})
    assert result["count"] == 1
    assert result["messages"][0] == {
        "id": 7,
        "channel": "HOG",
        "topic": "Integration",
        "sender": "Hermes Bot",
        "timestamp": 1735689600,
        "content": "Status updated",
        "permalink": "https://zulip.example.test/#narrow/near/7",
    }


def test_list_channels_returns_compact_channel_ids_and_names(monkeypatch):
    monkeypatch.setattr("tools.zulip_tool._api_request", lambda *a, **k: {
        "subscriptions": [{"stream_id": 1, "name": "HOG", "in_home_view": True}]
    })
    result = json.loads(_handle_list_channels({}))
    assert result["result"] == {"count": 1, "channels": [{"id": 1, "name": "HOG"}]}


def test_get_topics_resolves_channel_name_then_returns_topics(monkeypatch):
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/users/me/subscriptions":
            return {"subscriptions": [{"stream_id": 9, "name": "Infrastructure"}]}
        if path == "/api/v1/users/me/9/topics":
            return {"topics": [{"name": "Unraid", "max_id": 31}]}
        return {"message": {"id": 31, "timestamp": 1735689600}}

    monkeypatch.setattr("tools.zulip_tool._api_request", request)
    result = json.loads(_handle_get_topics({"channel": "Infrastructure"}))
    assert result["result"]["topics"][0] == {
        "name": "Unraid", "last_message_id": 31, "recent_activity_timestamp": 1735689600
    }
    assert any(path == "/api/v1/users/me/9/topics" for _, path, _ in calls)


def test_read_topic_uses_recent_narrow_and_default_limit(monkeypatch):
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["params"])
        return {"messages": []}

    monkeypatch.setattr("tools.zulip_tool._api_request", request)
    result = json.loads(_handle_read_topic({"channel": "HOG", "topic": "Zulip Integration"}))
    assert result["result"]["count"] == 0
    assert captured["anchor"] == "newest"
    assert captured["num_before"] == 30
    assert json.loads(captured["narrow"]) == [
        {"operator": "channel", "operand": "HOG"},
        {"operator": "topic", "operand": "Zulip Integration"},
    ]


def test_search_scopes_keyword_channel_and_topic(monkeypatch):
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["params"])
        return {"messages": []}

    monkeypatch.setattr("tools.zulip_tool._api_request", request)
    result = json.loads(_handle_search({"query": "restart approval", "channel": "HOG", "topic": "Integration"}))
    assert result["result"]["count"] == 0
    assert json.loads(captured["narrow"]) == [
        {"operator": "search", "operand": "restart approval"},
        {"operator": "channel", "operand": "HOG"},
        {"operator": "topic", "operand": "Integration"},
    ]


def test_send_message_returns_message_id_and_permalink(monkeypatch):
    captured = {}

    def request(method, path, **kwargs):
        captured.update(kwargs["data"])
        return {"id": 88}

    monkeypatch.setenv("ZULIP_URL", "https://zulip.example.test")
    monkeypatch.setattr("tools.zulip_tool._api_request", request)
    result = json.loads(_handle_send_message({"channel": "Hermes", "topic": "Integration Test", "content": "Hello"}))
    assert captured == {"type": "stream", "to": "Hermes", "topic": "Integration Test", "content": "Hello"}
    assert result["result"] == {"message_id": 88, "permalink": "https://zulip.example.test/#narrow/near/88"}


def test_handlers_reject_missing_required_values_without_network_call():
    assert "channel" in json.loads(_handle_get_topics({}))["error"]
    assert "topic" in json.loads(_handle_read_topic({"channel": "HOG"}))["error"]
    assert "query" in json.loads(_handle_search({}))["error"]
    assert "content" in json.loads(_handle_send_message({"channel": "HOG", "topic": "X"}))["error"]


def test_get_message_link_needs_no_network_call(monkeypatch):
    monkeypatch.setenv("ZULIP_URL", "https://zulip.example.test:8666/")
    result = json.loads(_handle_get_message_link({"message_id": 42}))
    assert result["result"] == {"message_id": 42, "permalink": "https://zulip.example.test:8666/#narrow/near/42"}


def test_get_message_link_rejects_an_invalid_message_id():
    result = json.loads(_handle_get_message_link({"message_id": "not-a-number"}))
    assert "error" in result


def test_check_available_requires_all_three_environment_variables(monkeypatch):
    for key in ("ZULIP_URL", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY"):
        monkeypatch.setenv(key, "configured")
    assert _check_zulip_available() is True
    monkeypatch.delenv("ZULIP_API_KEY")
    assert _check_zulip_available() is False


def test_all_capabilities_are_registered_under_the_zulip_toolset():
    from tools.registry import registry

    for name in (
        "zulip_get_message_link", "zulip_list_channels", "zulip_get_topics", "zulip_read_topic",
        "zulip_search", "zulip_send_message",
    ):
        entry = registry.get_entry(name)
        assert entry is not None
        assert entry.toolset == "zulip"


def test_zulip_is_part_of_telegram_core_toolset():
    from toolsets import resolve_toolset

    telegram_tools = resolve_toolset("hermes-telegram")
    assert "zulip_list_channels" in telegram_tools
    assert "zulip_send_message" in telegram_tools
