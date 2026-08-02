"""Slack media delivery for send_message.

Covers ``plugins/platforms/slack/adapter.py::_standalone_send`` media path:
text+file, media-only, caption-on-upload, missing-file warnings.

``slack_sdk`` is optional in CI, so tests inject a fake module into
``sys.modules`` (same pattern as ``tests/gateway/test_slack.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# The production module imports aiohttp eagerly although these media tests
# replace every external effect at the Slack SDK boundary. Keep that optional
# dependency explicit and injectable in the bare core test environment.
try:
    import aiohttp as _aiohttp  # noqa: F401
except ImportError:
    _aiohttp = ModuleType("aiohttp")
    _aiohttp.ClientSession = MagicMock()
    _aiohttp.ClientTimeout = MagicMock()
    sys.modules["aiohttp"] = _aiohttp

from plugins.platforms.slack.adapter import _standalone_send


def _pconfig(token: str = "xoxb-test"):
    return SimpleNamespace(token=token, extra={})


def _tmpfile(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"%PDF-1.4 test")
    f.close()
    return f.name


def _mock_client(*, post_ok=True, upload_ok=True):
    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        return_value={
            "ok": post_ok,
            "ts": "111.222",
            "error": None if post_ok else "channel_not_found",
        }
    )
    if upload_ok:
        client.files_upload_v2 = AsyncMock(
            return_value={
                "ok": True,
                "file": {
                    "id": "F123",
                    "timestamp": 1234567890,
                    "shares": {"public": {"C012AB3CD": [{"ts": "333.444"}]}},
                },
            }
        )
    else:
        client.files_upload_v2 = AsyncMock(
            return_value={"ok": False, "error": "not_in_channel"}
        )
    return client


@contextlib.contextmanager
def _fake_slack_sdk(client):
    """Make ``from slack_sdk.web.async_client import AsyncWebClient`` resolve to a factory."""
    sdk = ModuleType("slack_sdk")
    web = ModuleType("slack_sdk.web")
    async_client = ModuleType("slack_sdk.web.async_client")
    async_client.AsyncWebClient = MagicMock(return_value=client)
    sdk.web = web
    web.async_client = async_client

    modules = {
        "slack_sdk": sdk,
        "slack_sdk.web": web,
        "slack_sdk.web.async_client": async_client,
    }
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, prev in old.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def test_text_plus_pdf_uploads_via_files_upload_v2():
    pdf = _tmpfile(".pdf")
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "C012AB3CD",
                    "Here is the report",
                    media_files=[(pdf, False)],
                )
            )
        assert result["success"] is True
        assert result["platform"] == "slack"
        client.chat_postMessage.assert_awaited_once()
        client.files_upload_v2.assert_awaited_once()
        upload_kwargs = client.files_upload_v2.await_args.kwargs
        assert upload_kwargs["channel"] == "C012AB3CD"
        assert upload_kwargs["file"] == pdf
        assert upload_kwargs["filename"] == os.path.basename(pdf)
        assert upload_kwargs["initial_comment"] == ""
    finally:
        os.unlink(pdf)


def test_media_only_skips_text_post():
    pdf = _tmpfile(".pdf")
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "C012AB3CD",
                    "",
                    media_files=[(pdf, False)],
                )
            )
        assert result["success"] is True
        client.chat_postMessage.assert_not_awaited()
        client.files_upload_v2.assert_awaited_once()
    finally:
        os.unlink(pdf)


def test_missing_token_and_dependency_fail_before_provider_contact(monkeypatch):
    contacts = []

    result = asyncio.run(
        _standalone_send(
            _pconfig(token=""),
            "C012AB3CD",
            "hello",
            on_provider_contact=lambda: contacts.append("contact"),
        )
    )

    assert "error" in result
    assert contacts == []

    media = _tmpfile(".pdf")
    try:
        monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", None)
        result = asyncio.run(
            _standalone_send(
                _pconfig(),
                "C012AB3CD",
                "hello",
                media_files=[(media, False)],
                on_provider_contact=lambda: contacts.append("contact"),
            )
        )
        assert "error" in result
        assert contacts == []
    finally:
        os.unlink(media)


def test_contact_callback_fires_at_each_web_api_effect():
    pdf = _tmpfile(".pdf")
    events = []
    client = _mock_client()

    async def upload_url(**_kwargs):
        events.append("get-upload-url")
        return {"ok": True}

    async def upload_bytes(**_kwargs):
        events.append("upload-bytes")
        return SimpleNamespace(status=200)

    async def complete_upload(**_kwargs):
        events.append("complete-upload")
        return {"ok": True}

    client.files_getUploadURLExternal = AsyncMock(side_effect=upload_url)
    client._upload_file = AsyncMock(side_effect=upload_bytes)
    client.files_completeUploadExternal = AsyncMock(side_effect=complete_upload)

    async def post(**_kwargs):
        events.append("post")
        return {"ok": True, "ts": "111.222"}

    async def upload(**_kwargs):
        await client.files_getUploadURLExternal()
        await client._upload_file()
        await client.files_completeUploadExternal()
        return {"ok": True, "file": {"id": "F123", "timestamp": 123}}

    client.chat_postMessage.side_effect = post
    client.files_upload_v2.side_effect = upload
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "C012AB3CD",
                    "hello",
                    media_files=[(pdf, False)],
                    on_provider_contact=lambda: events.append("contact"),
                )
            )
        assert result["success"] is True
        assert events == [
            "contact", "post",
            "contact", "get-upload-url",
            "contact", "upload-bytes",
            "contact", "complete-upload",
        ]
    finally:
        os.unlink(pdf)


def test_send_to_platform_routes_slack_media():
    """_send_to_platform must call Slack standalone_sender with media_files."""
    import httpx

    if not hasattr(httpx, "Proxy") or not hasattr(httpx, "URL"):
        pytest.skip("httpx type annotations incompatible with telegram library")

    from gateway.config import Platform
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry
    from tools.send_message_tool import _send_to_platform

    pdf = _tmpfile(".pdf")
    discover_plugins()
    entry = platform_registry.get("slack")
    assert entry is not None and entry.standalone_sender_fn is not None
    original = entry.standalone_sender_fn
    mock_sender = AsyncMock(
        return_value={"success": True, "platform": "slack", "message_id": "1.2"}
    )
    entry.standalone_sender_fn = mock_sender
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                _pconfig(),
                "C012AB3CD",
                "Here is the report",
                media_files=[(pdf, False)],
            )
        )
        assert result["success"] is True
        mock_sender.assert_awaited()
        call_kwargs = mock_sender.await_args.kwargs
        assert call_kwargs.get("media_files") == [(pdf, False)]
        # Single captionable file + short text → caption rides the upload.
        assert call_kwargs.get("caption") == "Here is the report"
        assert not result.get("warnings")
    finally:
        entry.standalone_sender_fn = original
        os.unlink(pdf)
