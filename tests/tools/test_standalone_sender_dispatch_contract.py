"""Tool-layer forwarding contract for plugin standalone senders."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from tools.send_message_tool import (
    _registry_standalone_send,
    _send_to_platform,
    _send_via_adapter,
)


def test_registry_pre_contact_error_is_not_marked(monkeypatch):
    from gateway.platform_registry import PlatformEntry, platform_registry

    contacts = []

    async def sender(*_args, on_provider_contact=None, **_kwargs):
        return {"error": "token not configured"}

    platform_registry.register(
        PlatformEntry(
            name="precontactprobe",
            label="Pre-contact probe",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
            standalone_sender_fn=sender,
        )
    )
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    try:
        result = asyncio.run(
            _send_via_adapter(
                Platform("precontactprobe"),
                SimpleNamespace(token="", extra={}),
                "channel",
                "payload",
                on_provider_contact=lambda: contacts.append("contact"),
            )
        )
    finally:
        platform_registry.unregister("precontactprobe")

    assert result == {"error": "token not configured"}
    assert contacts == []


def test_direct_registry_helper_forwards_callback_without_outer_marking(monkeypatch):
    from gateway.platform_registry import PlatformEntry, platform_registry

    contacts = []
    seen_callback = []

    async def sender(*_args, on_provider_contact=None, **_kwargs):
        seen_callback.append(on_provider_contact)
        return {"error": "setup failed"}

    platform_registry.register(
        PlatformEntry(
            name="registrydirectprobe",
            label="Registry direct probe",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
            standalone_sender_fn=sender,
        )
    )
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    try:
        result = asyncio.run(
            _registry_standalone_send(
                "registrydirectprobe",
                SimpleNamespace(token="", extra={}),
                "channel",
                "payload",
                on_provider_contact=lambda: contacts.append("contact"),
            )
        )
    finally:
        platform_registry.unregister("registrydirectprobe")

    assert result == {"error": "setup failed"}
    assert len(seen_callback) == 1 and seen_callback[0] is not None
    assert contacts == []


def test_slack_text_forwards_contact_callback_through_adapter_fallback(monkeypatch):
    from gateway.platform_registry import PlatformEntry, platform_registry

    contacts = []

    async def sender(*_args, on_provider_contact=None, **_kwargs):
        assert on_provider_contact is not None
        on_provider_contact()
        raise RuntimeError("response lost after provider acceptance")

    original = platform_registry.get("slack")
    if original is not None:
        platform_registry.unregister("slack")
    platform_registry.register(
        PlatformEntry(
            name="slack",
            label="Slack",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
            standalone_sender_fn=sender,
        )
    )
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                SimpleNamespace(token="", extra={}),
                "C123",
                "payload",
                on_provider_contact=lambda: contacts.append("contact"),
            )
        )
    finally:
        platform_registry.unregister("slack")
        if original is not None:
            platform_registry.register(original)

    assert result == {
        "error": "Plugin standalone send failed: response lost after provider acceptance",
    }
    assert contacts == ["contact"]


@pytest.mark.parametrize(
    ("message", "media_count", "expected_caption"),
    [("native caption", 1, "native caption"), ("separate text", 2, None)],
)
def test_whatsapp_media_paths_forward_callback_without_outer_marking(
    monkeypatch, tmp_path, message, media_count, expected_caption,
):
    from gateway.platform_registry import PlatformEntry, platform_registry

    contacts = []
    media = []
    for index in range(media_count):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"image")
        media.append((str(path), False))

    sender = AsyncMock(return_value={"success": True, "message_id": "wa-1"})
    original = platform_registry.get("whatsapp")
    if original is not None:
        platform_registry.unregister("whatsapp")
    platform_registry.register(
        PlatformEntry(
            name="whatsapp",
            label="WhatsApp",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
            standalone_sender_fn=sender,
            max_message_length=4096,
        )
    )
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.WHATSAPP,
                SimpleNamespace(token="", extra={}),
                "12345",
                message,
                media_files=media,
                on_provider_contact=lambda: contacts.append("contact"),
            )
        )
    finally:
        platform_registry.unregister("whatsapp")
        if original is not None:
            platform_registry.register(original)

    assert result["success"] is True
    assert contacts == []
    call = sender.await_args
    assert call.kwargs["on_provider_contact"] is not None
    assert call.kwargs.get("caption") == expected_caption
