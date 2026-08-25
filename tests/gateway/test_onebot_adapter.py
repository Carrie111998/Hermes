"""Tests for the OneBot v11 platform adapter plugin."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_mod = load_plugin_adapter("onebot")

_normalize_text = _mod._normalize_text
_parse_ws_url = _mod._parse_ws_url
OneBotAdapter = _mod.OneBotAdapter
check_requirements = _mod.check_requirements
validate_config = _mod.validate_config
register = _mod.register
_env_enablement = _mod._env_enablement


# ── Protocol helpers ─────────────────────────────────────────────────────

class TestNormalizeText:

    def test_plain_string(self):
        assert _normalize_text("hello") == "hello"

    def test_text_segment(self):
        assert _normalize_text([{"type": "text", "data": {"text": "你好"}}]) == "你好"

    def test_mixed_segments(self):
        msg = [
            {"type": "text", "data": {"text": "早上好"}},
            {"type": "face", "data": {"id": "1"}},
            {"type": "at", "data": {"qq": "123"}},
        ]
        assert _normalize_text(msg) == "早上好[表情]@123"

    def test_voice_and_image_placeholders(self):
        msg = [
            {"type": "image", "data": {}},
            {"type": "record", "data": {}},
            {"type": "text", "data": {"text": "结尾"}},
        ]
        assert _normalize_text(msg) == "[图片][语音]结尾"

    def test_unknown_segment_type(self):
        assert _normalize_text([{"type": "dice", "data": {}}]) == "[dice]"


class TestParseWsUrl:

    def test_host_port(self):
        assert _parse_ws_url("ws://127.0.0.1:3001") == ("127.0.0.1", 3001)

    def test_root_path(self):
        assert _parse_ws_url("ws://example.com") == ("example.com", 3001)

    def test_no_scheme_defaults(self):
        # No "ws://" prefix → regex doesn't match → returns defaults
        assert _parse_ws_url("localhost:9999") == ("127.0.0.1", 3001)

    def test_invalid_input(self):
        assert _parse_ws_url("") == ("127.0.0.1", 3001)


# ── Adapter init ─────────────────────────────────────────────────────────

def _make_config(extra=None):
    from gateway.config import PlatformConfig
    return PlatformConfig(
        enabled=True,
        extra=extra or {
            "ws_url": "ws://127.0.0.1:3001",
            "http_url": "http://127.0.0.1:3000",
        },
    )


class TestOneBotAdapterInit:

    def test_init_from_config(self, monkeypatch):
        for key in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL", "ONEBOT_WS_MODE", "ONEBOT_ALLOW_FROM"):
            monkeypatch.delenv(key, raising=False)
        adapter = OneBotAdapter(_make_config())
        assert adapter.ws_url == "ws://127.0.0.1:3001"
        assert adapter.http_url == "http://127.0.0.1:3000"
        assert adapter.ws_mode == "forward"
        assert adapter.require_at is True

    def test_allow_from_config(self, monkeypatch):
        for key in ("ONEBOT_ALLOW_FROM", "ONEBOT_GROUP_ALLOW_FROM", "ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(key, raising=False)
        adapter = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                              "allow_from": ["100010001", "100010002"],
                                              "group_allow_from": ["123"]}))
        assert adapter.allow_from == {"100010001", "100010002"}
        assert adapter.group_allow_from == {"123"}

    def test_allow_from_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_ALLOW_FROM", "999,888")
        monkeypatch.delenv("ONEBOT_GROUP_ALLOW_FROM", raising=False)
        adapter = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                              "allow_from": ["100010001"]}))
        # env wins over config
        assert adapter.allow_from == {"999", "888"}

    def test_env_enablement_requires_both_urls(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.delenv("ONEBOT_HTTP_URL", raising=False)
        assert _env_enablement() is None

    def test_env_enablement_with_both(self, monkeypatch):
        monkeypatch.setenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        monkeypatch.setenv("ONEBOT_ALLOW_FROM", "111")
        seed = _env_enablement()
        assert seed is not None
        assert seed["ws_url"] == "ws://127.0.0.1:3001"
        assert seed["allow_from"] == ["111"]


# ── send_voice path mapping ──────────────────────────────────────────────

class TestSendVoiceMapping:

    def test_voice_mount_maps_host_to_container(self, monkeypatch):
        for key in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(key, raising=False)
        real = OneBotAdapter(_make_config({
            "ws_url": "ws://x", "http_url": "http://y",
            "voice_mount": {"host": "C:/hermes/data",
                            "container": "/app/hermes"},
        }))
        # Patch the HTTP layer to capture the CQ message
        captured = {}
        async def fake_http(action, payload):
            captured["message"] = payload.get("message", "")
            return {"retcode": 0, "data": {"message_id": 12345}}
        real._http = fake_http
        result = asyncio.run(real.send_voice(
            "100010001",
            "C:/hermes/data/audio_cache/voice.wav",
        ))
        assert result.success
        assert captured["message"] == "[CQ:record,file=/app/hermes/audio_cache/voice.wav]"

    def test_standalone_send_media_tuple(self, monkeypatch):
        """media_files 元素是 (path, is_voice) 元组，必须解包并生成 CQ record。"""
        from gateway.config import PlatformConfig
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        cfg = PlatformConfig(enabled=True, extra={
            "http_url": "http://127.0.0.1:3000",
            "voice_mount": {"host": "C:/hermes/data/audio_cache",
                            "container": "/app/hermes-audio"},
        })
        sent = []
        async def fake_post(action, payload):
            sent.append(payload.get("message", ""))
            return {"retcode": 0, "data": {"message_id": 1}}
        import asyncio
        # Patch aiohttp by swapping _post via direct function injection
        import types
        # Use the module's own _post indirectly: monkeypatch aiohttp.ClientSession via
        # a fake that records calls. Simpler: test the mapping logic by calling
        # the module-level helper we extract inline.
        from io import StringIO
        # Directly call standalone_send with a stubbed aiohttp
        orig_session = None
        try:
            import aiohttp
        except ImportError:
            aiohttp = None
        if aiohttp is None:
            return  # skip if no aiohttp (shouldn't happen in hermes venv)
        # Patch the adapter module's aiohttp.ClientSession with a fake
        from unittest.mock import patch as mp
        calls = []

        class FakeResp:
            def __init__(self, data):
                self._data = data
            async def json(self):
                return self._data
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def __init__(self):
                self._closed = False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                self._closed = True
                return False
            def post(self, url, json=None, headers=None, timeout=None):
                calls.append((url, json))
                return FakeResp({"retcode": 0, "data": {"message_id": 1}})

        onebot_mod = _mod
        with mp.object(onebot_mod.aiohttp, "ClientSession", FakeSession):
            result = asyncio.run(onebot_mod._standalone_send(
                cfg, "100010001", "提醒文本",
                media_files=[("C:/hermes/data/audio_cache/voice.ogg", True)],
            ))
        assert result.get("success") is True
        # calls[0] = text, calls[1] = media record
        assert len(calls) >= 2, f"expected text+media calls, got {len(calls)}"
        media_msg = calls[1][1]["message"]
        assert media_msg == "[CQ:record,file=/app/hermes-audio/voice.ogg]"

    def test_standalone_send_media_backslash_path(self, monkeypatch):
        """Windows 反斜杠路径（media_files 实际格式）也必须正确映射到容器路径。"""
        from gateway.config import PlatformConfig
        monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
        cfg = PlatformConfig(enabled=True, extra={
            "http_url": "http://127.0.0.1:3000",
            "voice_mount": {"host": "C:/hermes/data/audio_cache",
                            "container": "/app/hermes-audio"},
        })
        from unittest.mock import patch as mp
        calls = []

        class FakeResp:
            def __init__(self, data):
                self._data = data
            async def json(self):
                return self._data
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def __init__(self):
                self._closed = False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                self._closed = True
                return False
            def post(self, url, json=None, headers=None, timeout=None):
                calls.append((url, json))
                return FakeResp({"retcode": 0, "data": {"message_id": 1}})

        onebot_mod = _mod
        with mp.object(onebot_mod.aiohttp, "ClientSession", FakeSession):
            result = asyncio.run(onebot_mod._standalone_send(
                cfg, "100010001", "提醒文本",
                # 反斜杠路径（cron/MEDIA 标记实际产生）
                media_files=[(r"C:\hermes\data\audio_cache\voice.ogg", True)],
            ))
        assert result.get("success") is True
        assert len(calls) >= 2
        media_msg = calls[1][1]["message"]
        assert media_msg == "[CQ:record,file=/app/hermes-audio/voice.ogg]"

    def test_no_mount_uses_raw_path(self, monkeypatch):
        for key in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(key, raising=False)
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y"}))
        captured = {}
        async def fake_http(action, payload):
            captured["message"] = payload.get("message", "")
            return {"retcode": 0, "data": {"message_id": 1}}
        real._http = fake_http
        result = asyncio.run(real.send_voice("100010001", "C:/hermes/tmp/voice.wav"))
        assert result.success
        assert captured["message"] == "[CQ:record,file=C:/hermes/tmp/voice.wav]"

    def test_metadata_voice_path_overrides(self, monkeypatch):
        for key in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(key, raising=False)
        real = OneBotAdapter(_make_config({
            "ws_url": "ws://x", "http_url": "http://y",
            "voice_mount": {"host": "C:/hermes/data", "container": "/app"},
        }))
        captured = {}
        async def fake_http(action, payload):
            captured["message"] = payload.get("message", "")
            return {"retcode": 0, "data": {"message_id": 2}}
        real._http = fake_http
        result = asyncio.run(real.send_voice(
            "100010001", "C:/hermes/audio_cache/x.wav",
            metadata={"voice_path": "/custom/x.wav"},
        ))
        assert result.success
        assert captured["message"] == "[CQ:record,file=/custom/x.wav]"


# ── Whitelist / authorization logic ──────────────────────────────────────

class TestWhitelist:

    def test_whitelisted_dm_passes(self):
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                           "allow_from": ["100010001"]}))
        real._emit_event = AsyncMock()
        asyncio.run(real._handle_private("100010001", "u", "hi", "m1"))
        real._emit_event.assert_called_once()
        # chat_id should equal the user id
        kwargs = real._emit_event.call_args.kwargs
        assert kwargs["chat_id"] == "100010001"
        assert kwargs["chat_type"] == "dm"

    def test_non_whitelisted_dm_ignored(self):
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                           "allow_from": ["100010001"]}))
        real._emit_event = AsyncMock()
        asyncio.run(real._handle_private("999999", "u", "hi", "m1"))
        real._emit_event.assert_not_called()

    def test_no_allow_from_means_deny_all(self):
        # Empty allow_from = fail closed
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                           "allow_from": []}))
        real._emit_event = AsyncMock()
        asyncio.run(real._handle_private("100010001", "u", "hi", "m1"))
        real._emit_event.assert_not_called()


# ── require_at logic ─────────────────────────────────────────────────────

class TestRequireAt:

    def test_group_requires_at_mention(self):
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                           "group_allow_from": ["123"]}))
        real._emit_event = AsyncMock()
        evt = {
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "self_id": 100010003,
        }
        asyncio.run(real._handle_group("123", "100010001", "u", "hello", "m1", evt))
        real._emit_event.assert_not_called()  # no @mention, dropped

    def test_group_at_self_passes(self):
        real = OneBotAdapter(_make_config({"ws_url": "ws://x", "http_url": "http://y",
                                           "group_allow_from": ["123"]}))
        real._emit_event = AsyncMock()
        evt = {
            "message": [{"type": "at", "data": {"qq": "100010003"}},
                        {"type": "text", "data": {"text": "hi"}}],
            "self_id": 100010003,
        }
        asyncio.run(real._handle_group("123", "100010001", "u", "hi", "m1", evt))
        real._emit_event.assert_called_once()


# ── register ─────────────────────────────────────────────────────────────

class TestRegister:

    def test_register_platform(self):
        calls = []
        class FakeCtx:
            def register_platform(self, **kw):
                calls.append(kw)
        register(FakeCtx())
        assert calls, "register_platform should be called"
        kw = calls[0]
        assert kw["name"] == "onebot"
        assert kw["label"] == "OneBot 11 (QQ)"
        assert "ONEBOT_WS_URL" in kw["required_env"]
        assert kw["cron_deliver_env_var"] == "ONEBOT_HOME_CHANNEL"
        assert kw["allowed_users_env"] == "ONEBOT_ALLOW_FROM"
        # adapter_factory should construct an adapter
        adapter = kw["adapter_factory"](_make_config())
        assert isinstance(adapter, OneBotAdapter)


# ── config validation ────────────────────────────────────────────────────

class TestValidateConfig:

    def test_forward_mode_requires_ws_url(self, monkeypatch):
        for k in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(k, raising=False)
        cfg = _make_config({"http_url": "http://y", "ws_mode": "forward"})  # no ws_url
        assert validate_config(cfg) is False

    def test_valid_config(self, monkeypatch):
        for k in ("ONEBOT_WS_URL", "ONEBOT_HTTP_URL"):
            monkeypatch.delenv(k, raising=False)
        cfg = _make_config({"ws_url": "ws://127.0.0.1:3001", "http_url": "http://127.0.0.1:3000"})
        assert validate_config(cfg) is True


# ── reconnect ──────────────────────────────────────────────────────────────
