"""WhatsApp media delivery for send_message (#19105).

Covers two layers:

* ``_bridge_media_type`` — extension/voice/force_document -> bridge mediaType.
* ``_standalone_send`` — text-first then per-file ``/send-media`` uploads,
  media-only (skip ``/send``), and missing-file errors. The bridge HTTP calls
  are mocked at the ``aiohttp.ClientSession`` boundary.
"""

import asyncio
import os
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# _standalone_send imports aiohttp lazily and every request is replaced below.
# Supply the optional module only for this mocked boundary in core-only test
# environments; real delivery still reports the missing dependency normally.
try:
    import aiohttp as _aiohttp  # noqa: F401
except ImportError:
    _aiohttp = ModuleType("aiohttp")
    _aiohttp.ClientSession = MagicMock()
    _aiohttp.ClientTimeout = MagicMock()
    sys.modules["aiohttp"] = _aiohttp

from plugins.platforms.whatsapp.adapter import _bridge_media_type, _standalone_send


# ---------------------------------------------------------------------------
# _bridge_media_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,is_voice,force_document,expected",
    [
        ("a.png", False, False, "image"),
        ("a.JPG", False, False, "image"),
        ("a.jpeg", False, False, "image"),
        ("a.webp", False, False, "image"),
        ("a.gif", False, False, "image"),
        ("a.mp4", False, False, "video"),
        ("a.mov", False, False, "video"),
        ("a.webm", False, False, "video"),
        ("a.ogg", True, False, "audio"),
        ("a.opus", False, False, "audio"),
        ("a.mp3", False, False, "audio"),
        ("a.wav", False, False, "audio"),
        ("a.pdf", False, False, "document"),
        ("a.zip", False, False, "document"),
        # force_document overrides everything
        ("a.png", False, True, "document"),
        ("a.mp4", False, True, "document"),
        # is_voice wins over a video extension
        ("a.mp4", True, False, "audio"),
    ],
)
def test_bridge_media_type(path, is_voice, force_document, expected):
    assert _bridge_media_type(path, is_voice, force_document) == expected


# ---------------------------------------------------------------------------
# _standalone_send — bridge HTTP mocked
# ---------------------------------------------------------------------------


def _resp(status, json_data=None, text_data=None):
    r = AsyncMock()
    r.status = status
    r.json = AsyncMock(return_value=json_data or {})
    r.text = AsyncMock(return_value=text_data or "")
    return r


def _session_with(responses):
    """Build a mocked aiohttp.ClientSession that returns *responses* in order
    and records every POST (url, json_payload)."""
    calls = []
    idx = [0]

    def _post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        r = responses[idx[0]] if idx[0] < len(responses) else responses[-1]
        idx[0] += 1
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=r)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx, calls


def _pconfig():
    return SimpleNamespace(token="", extra={"bridge_port": 3000})


def _tmpfile(suffix):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"x")
    f.close()
    return f.name


def test_text_plus_mixed_media_routes_native_types():
    img = _tmpfile(".png")
    vid = _tmpfile(".mp4")
    voice = _tmpfile(".ogg")
    try:
        session_ctx, calls = _session_with(
            [
                _resp(200, {"messageId": "t1"}),
                _resp(200, {"messageId": "m1"}),
                _resp(200, {"messageId": "m2"}),
                _resp(200, {"messageId": "m3"}),
            ]
        )
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            res = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "12345",
                    "hello",
                    media_files=[(img, False), (vid, False), (voice, True)],
                )
            )
        assert res["success"] is True
        # text first, then three media uploads in order
        assert calls[0][0].endswith("/send")
        assert calls[0][1]["message"] == "hello"
        media_types = [c[1]["mediaType"] for c in calls if c[0].endswith("/send-media")]
        assert media_types == ["image", "video", "audio"]
        # chat id normalized to a WhatsApp JID
        assert "@" in calls[0][1]["chatId"]
    finally:
        for p in (img, vid, voice):
            os.unlink(p)


def test_missing_captioned_file_falls_back_to_text():
    """If the single captioned file is missing, the caption is delivered as a
    plain /send message rather than being silently lost (W1)."""
    session_ctx, calls = _session_with([_resp(200, {"messageId": "t1"})])
    with patch("aiohttp.ClientSession", return_value=session_ctx):
        res = asyncio.run(
            _standalone_send(
                _pconfig(),
                "12345",
                "",
                media_files=[("/no/such/file.png", False)],
                caption="floor plan",
            )
        )
    # The send still surfaces the missing-file error...
    assert "error" in res
    assert "not found" in res["error"]
    # ...but the caption text was delivered on its own first.
    assert len(calls) == 1
    assert calls[0][0].endswith("/send")
    assert calls[0][1]["message"] == "floor plan"


@pytest.mark.parametrize("caption", [None, "native caption"])
def test_contact_callback_fires_at_each_bridge_request(caption):
    image = _tmpfile(".png")
    events = []
    try:
        session_ctx, calls = _session_with(
            [_resp(200, {"messageId": "text"}), _resp(200, {"messageId": "media"})]
        )
        session = session_ctx.__aenter__.return_value
        original_post = session.post.side_effect

        def recording_post(*args, **kwargs):
            events.append("request")
            return original_post(*args, **kwargs)

        session.post.side_effect = recording_post
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "12345",
                    "hello" if caption is None else "",
                    media_files=[(image, False)],
                    caption=caption,
                    on_provider_contact=lambda: events.append("contact"),
                )
            )

        assert result["success"] is True
        expected_requests = 2 if caption is None else 1
        assert len(calls) == expected_requests
        assert events == [item for _ in range(expected_requests) for item in ("contact", "request")]
    finally:
        os.unlink(image)
