"""Lightweight native Zulip channel/topic capability.

This module deliberately implements only focused read/search/send operations;
it is not a Hermes messaging adapter and does not mirror Telegram messages.
Credentials are read at request time from ZULIP_URL, ZULIP_BOT_EMAIL, and
ZULIP_API_KEY. They are never persisted or returned in tool output.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_DEFAULT_MESSAGE_LIMIT = 30
_MAX_MESSAGE_LIMIT = 100
_REQUEST_TIMEOUT_SECONDS = 20.0


def _get_config() -> tuple[str, str, str]:
    """Load Zulip configuration only from the running process environment."""
    return (
        os.getenv("ZULIP_URL", "").rstrip("/"),
        os.getenv("ZULIP_BOT_EMAIL", ""),
        os.getenv("ZULIP_API_KEY", ""),
    )


def _check_zulip_available() -> bool:
    """Expose the tools only when the complete Zulip credential set exists."""
    return all(_get_config())


def _build_permalink(message_id: int) -> str:
    """Build a stable Zulip permalink from the configured organization URL."""
    base_url, _, _ = _get_config()
    return f"{base_url}/#narrow/near/{int(message_id)}"


def _api_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Call a Zulip REST endpoint using HTTP Basic auth without logging secrets."""
    base_url, email, api_key = _get_config()
    if not all((base_url, email, api_key)):
        raise RuntimeError("Zulip is not configured: ZULIP_URL, ZULIP_BOT_EMAIL, and ZULIP_API_KEY are required")

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
        response = client.request(
            method=method,
            url=f"{base_url}{path}",
            params=params,
            data=data,
            auth=(email, api_key),
            headers={"User-Agent": "Hermes-Agent-Zulip/1.0"},
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Zulip returned an invalid JSON response")
    if payload.get("result") == "error":
        raise RuntimeError(payload.get("msg", "Zulip API returned an error"))
    return payload


def _required_string(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required parameter: {name}")
    return value.strip()


def _message_limit(value: Any) -> int:
    if value is None:
        return _DEFAULT_MESSAGE_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= _MAX_MESSAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_MESSAGE_LIMIT}")
    return limit


def _normalize_messages(payload: dict[str, Any]) -> dict[str, Any]:
    """Return compact, Telegram-friendly message records from Zulip output."""
    messages = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("id"), int):
            continue
        messages.append({
            "id": message["id"],
            "channel": message.get("display_recipient", ""),
            "topic": message.get("topic", message.get("subject", "")),
            "sender": message.get("sender_full_name", message.get("sender_email", "")),
            "timestamp": message.get("timestamp"),
            "content": message.get("content", message.get("raw_content", "")),
            "permalink": _build_permalink(message["id"]),
        })
    return {"count": len(messages), "messages": messages}


def _normalize_topics(
    payload: dict[str, Any],
    recent_activity: Optional[dict[int, Any]] = None,
) -> dict[str, Any]:
    """Return topic records; timestamp enrichment is added when fetched."""
    topics = []
    for topic in payload.get("topics", []):
        if not isinstance(topic, dict):
            continue
        entry = {"name": topic.get("name", ""), "last_message_id": topic.get("max_id")}
        if recent_activity and isinstance(topic.get("max_id"), int) and topic["max_id"] in recent_activity:
            entry["recent_activity_timestamp"] = recent_activity[topic["max_id"]]
        topics.append(entry)
    return {"count": len(topics), "topics": topics}


def _subscriptions() -> list[dict[str, Any]]:
    payload = _api_request("GET", "/api/v1/users/me/subscriptions")
    subscriptions = payload.get("subscriptions", [])
    return [item for item in subscriptions if isinstance(item, dict)]


def _channel_id_by_name(channel: str) -> int:
    folded = channel.casefold()
    matches = [item for item in _subscriptions() if str(item.get("name", "")).casefold() == folded]
    if not matches:
        raise ValueError(f"Zulip channel not found or inaccessible: {channel}")
    stream_id = matches[0].get("stream_id")
    if not isinstance(stream_id, int):
        raise RuntimeError(f"Zulip returned an invalid stream ID for channel: {channel}")
    return stream_id


def _topic_activity_timestamps(topics_payload: dict[str, Any]) -> dict[int, Any]:
    """Fetch timestamps of each topic's last message when the API permits it."""
    timestamps: dict[int, Any] = {}
    for topic in topics_payload.get("topics", []):
        if not isinstance(topic, dict) or not isinstance(topic.get("max_id"), int):
            continue
        message_id = topic["max_id"]
        try:
            response = _api_request(
                "GET", f"/api/v1/messages/{message_id}",
                params={"apply_markdown": "false", "allow_empty_topic_name": "true"},
            )
            message = response.get("message", {})
            if isinstance(message, dict) and message.get("timestamp") is not None:
                timestamps[message_id] = message["timestamp"]
        except (httpx.HTTPError, RuntimeError):
            # Topic listing remains useful if timestamp enrichment is unavailable.
            logger.debug("Zulip topic activity timestamp unavailable for message_id=%s", message_id)
    return timestamps


def _handle_get_message_link(args: dict, **_: Any) -> str:
    """Return a stable Zulip permalink for a known message ID without network I/O."""
    try:
        raw_message_id = args.get("message_id")
        if isinstance(raw_message_id, bool) or not isinstance(raw_message_id, (int, str)):
            raise ValueError("message_id must be a positive integer")
        message_id = int(raw_message_id)
        if message_id <= 0:
            raise ValueError("message_id must be a positive integer")
        return json.dumps({"result": {"message_id": message_id, "permalink": _build_permalink(message_id)}})
    except (TypeError, ValueError) as exc:
        return tool_error(f"Failed to build Zulip message link: {exc}")


def _handle_list_channels(args: dict, **_: Any) -> str:
    try:
        channels = [
            {"id": item.get("stream_id"), "name": item.get("name", "")}
            for item in _subscriptions()
            if isinstance(item.get("stream_id"), int)
        ]
        return json.dumps({"result": {"count": len(channels), "channels": channels}})
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("zulip_list_channels failed: %s", exc)
        return tool_error(f"Failed to list Zulip channels: {exc}")


def _handle_get_topics(args: dict, **_: Any) -> str:
    try:
        channel = _required_string(args, "channel")
        stream_id = _channel_id_by_name(channel)
        payload = _api_request(
            "GET", f"/api/v1/users/me/{stream_id}/topics",
            params={"allow_empty_topic_name": "true"},
        )
        activity = _topic_activity_timestamps(payload)
        return json.dumps({"result": _normalize_topics(payload, activity)})
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        logger.warning("zulip_get_topics failed: %s", exc)
        return tool_error(f"Failed to get Zulip topics: {exc}")


def _handle_read_topic(args: dict, **_: Any) -> str:
    try:
        channel = _required_string(args, "channel")
        topic = _required_string(args, "topic")
        limit = _message_limit(args.get("limit"))
        payload = _api_request(
            "GET", "/api/v1/messages",
            params={
                "anchor": "newest",
                "num_before": limit,
                "num_after": 0,
                "apply_markdown": "false",
                "allow_empty_topic_name": "true",
                "narrow": json.dumps([
                    {"operator": "channel", "operand": channel},
                    {"operator": "topic", "operand": topic},
                ]),
            },
        )
        return json.dumps({"result": _normalize_messages(payload)})
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        logger.warning("zulip_read_topic failed: %s", exc)
        return tool_error(f"Failed to read Zulip topic: {exc}")


def _handle_search(args: dict, **_: Any) -> str:
    try:
        query = _required_string(args, "query")
        limit = _message_limit(args.get("limit"))
        narrow = [{"operator": "search", "operand": query}]
        if args.get("channel") is not None:
            narrow.append({"operator": "channel", "operand": _required_string(args, "channel")})
        if args.get("topic") is not None:
            narrow.append({"operator": "topic", "operand": _required_string(args, "topic")})
        payload = _api_request(
            "GET", "/api/v1/messages",
            params={
                "anchor": "newest",
                "num_before": limit,
                "num_after": 0,
                "apply_markdown": "false",
                "allow_empty_topic_name": "true",
                "narrow": json.dumps(narrow),
            },
        )
        return json.dumps({"result": _normalize_messages(payload)})
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        logger.warning("zulip_search failed: %s", exc)
        return tool_error(f"Failed to search Zulip: {exc}")


def _handle_send_message(args: dict, **_: Any) -> str:
    try:
        channel = _required_string(args, "channel")
        topic = _required_string(args, "topic")
        content = _required_string(args, "content")
        payload = _api_request(
            "POST", "/api/v1/messages",
            data={"type": "stream", "to": channel, "topic": topic, "content": content},
        )
        message_id = payload.get("id")
        if not isinstance(message_id, int):
            raise RuntimeError("Zulip send response did not include a message ID")
        return json.dumps({"result": {"message_id": message_id, "permalink": _build_permalink(message_id)}})
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        logger.warning("zulip_send_message failed: %s", exc)
        return tool_error(f"Failed to send Zulip message: {exc}")


ZULIP_GET_MESSAGE_LINK_SCHEMA = {
    "name": "zulip_get_message_link",
    "description": "Build a stable Zulip permalink for a known message ID without reading or changing any message.",
    "parameters": {"type": "object", "properties": {"message_id": {"type": "integer", "description": "Positive Zulip message ID."}}, "required": ["message_id"]},
}
ZULIP_LIST_CHANNELS_SCHEMA = {
    "name": "zulip_list_channels",
    "description": "List Zulip channels the configured Hermes bot can access. Returns channel IDs and names.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}
ZULIP_GET_TOPICS_SCHEMA = {
    "name": "zulip_get_topics",
    "description": "List topics in a Zulip channel by name, ordered by recent activity. Returns topic names and latest activity timestamps when available.",
    "parameters": {"type": "object", "properties": {"channel": {"type": "string", "description": "Zulip channel name, for example 'HOG' or 'Infrastructure'."}}, "required": ["channel"]},
}
ZULIP_READ_TOPIC_SCHEMA = {
    "name": "zulip_read_topic",
    "description": "Read recent messages in one Zulip Channel + Topic. Never fetches the whole topic history by default.",
    "parameters": {"type": "object", "properties": {"channel": {"type": "string"}, "topic": {"type": "string"}, "limit": {"type": "integer", "description": "Recent message count, 1-100; default 30."}}, "required": ["channel", "topic"]},
}
ZULIP_SEARCH_SCHEMA = {
    "name": "zulip_search",
    "description": "Search Zulip messages using the official Zulip search narrow. Optionally scope the search to a channel and topic.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "channel": {"type": "string"}, "topic": {"type": "string"}, "limit": {"type": "integer", "description": "Result count, 1-100; default 30."}}, "required": ["query"]},
}
ZULIP_SEND_MESSAGE_SCHEMA = {
    "name": "zulip_send_message",
    "description": "Send a message to a Zulip channel and topic. A new topic is created by sending its first message; do not create topics speculatively.",
    "parameters": {"type": "object", "properties": {"channel": {"type": "string"}, "topic": {"type": "string"}, "content": {"type": "string"}}, "required": ["channel", "topic", "content"]},
}

registry.register(
    name="zulip_get_message_link", toolset="zulip", schema=ZULIP_GET_MESSAGE_LINK_SCHEMA,
    handler=_handle_get_message_link, check_fn=_check_zulip_available, emoji="🟩",
)
registry.register(
    name="zulip_list_channels", toolset="zulip", schema=ZULIP_LIST_CHANNELS_SCHEMA,
    handler=_handle_list_channels, check_fn=_check_zulip_available, emoji="🟩",
)
registry.register(
    name="zulip_get_topics", toolset="zulip", schema=ZULIP_GET_TOPICS_SCHEMA,
    handler=_handle_get_topics, check_fn=_check_zulip_available, emoji="🟩",
)
registry.register(
    name="zulip_read_topic", toolset="zulip", schema=ZULIP_READ_TOPIC_SCHEMA,
    handler=_handle_read_topic, check_fn=_check_zulip_available, emoji="🟩",
)
registry.register(
    name="zulip_search", toolset="zulip", schema=ZULIP_SEARCH_SCHEMA,
    handler=_handle_search, check_fn=_check_zulip_available, emoji="🟩",
)
registry.register(
    name="zulip_send_message", toolset="zulip", schema=ZULIP_SEND_MESSAGE_SCHEMA,
    handler=_handle_send_message, check_fn=_check_zulip_available, emoji="🟩",
)
