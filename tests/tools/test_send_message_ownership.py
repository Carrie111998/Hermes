"""Ownership fences for standalone cron delivery transport."""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import discover_plugins
from tools.send_message_tool import _send_to_platform


def test_ownership_loss_after_first_chunk_stops_later_chunk_and_media():
    """A stale standalone attempt must not continue its internal fanout."""
    discover_plugins()
    entry = platform_registry.get("discord")
    assert entry is not None
    original = entry.standalone_sender_fn
    owned = True
    sent = []

    async def send(_pconfig, _chat_id, message, **kwargs):
        nonlocal owned
        sent.append((message, kwargs.get("media_files")))
        owned = False
        return {"success": True, "message_id": "1"}

    entry.standalone_sender_fn = send
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.DISCORD,
                SimpleNamespace(enabled=True, token="***", extra={}),
                "ch",
                "word " * 1000,
                media_files=[("/tmp/attachment.png", False)],
                ownership_guard=lambda: owned,
            )
        )
    finally:
        entry.standalone_sender_fn = original

    assert result["error"] == "Delivery ownership lost during standalone send"
    assert len(sent) == 1
    assert sent[0][1] == []


def test_single_chunk_plugin_stops_after_first_media_item():
    """Guarded media iteration fences plugin-owned attachment fanout."""
    discover_plugins()
    entry = platform_registry.get("discord")
    assert entry is not None
    original = entry.standalone_sender_fn
    owned = True
    sent_media = []

    async def send(_pconfig, _chat_id, _message, **kwargs):
        nonlocal owned
        for media_item in kwargs["media_files"]:
            sent_media.append(media_item)
            owned = False
        return {"success": True, "message_id": "1"}

    entry.standalone_sender_fn = send
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.DISCORD,
                SimpleNamespace(enabled=True, token="***", extra={}),
                "ch",
                "short body",
                media_files=[
                    ("/tmp/first.png", False),
                    ("/tmp/second.png", False),
                ],
                ownership_guard=lambda: owned,
            )
        )
    finally:
        entry.standalone_sender_fn = original

    assert result["error"] == "Delivery ownership lost during standalone send"
    assert sent_media == [("/tmp/first.png", False)]
