"""Tests for the Telnyx WebSocket TTS provider in tools/tts_tool.py.

Mocks the ``websockets`` library so no network or API key is needed.
Validates registration, constants, the function signature, the full
WebSocket protocol sequence, output file contents, and error paths.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

from tools import tts_tool
from tools.tts_tool import (
    BUILTIN_TTS_PROVIDERS,
    DEFAULT_TELNYX_VOICE,
    PROVIDER_MAX_TEXT_LENGTH,
    TELNYX_FALLBACK_VOICES,
    TELNYX_TTS_DEFAULT_BASE_URL,
    TELNYX_TTS_VOICE_FAMILIES,
    _generate_telnyx_tts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_CHUNK_1 = b"FAKEMP3CHUNK1"
FAKE_CHUNK_2 = b"FAKEMP3CHUNK2"


def _make_fake_websockets(messages):
    """Return a (fake_module, ws_instance) pair.

    ``connect`` returns a fake async context-manager WebSocket that yields
    *messages* and records everything sent to it.
    """

    class _FakeWS:
        def __init__(self):
            self._iter = iter(messages)
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    instance = _FakeWS()
    fake_module = types.ModuleType("websockets")
    fake_module.connect = MagicMock(return_value=instance)
    return fake_module, instance


@pytest.fixture
def env_api_key(monkeypatch):
    """Make ``get_env_value`` read ``os.environ`` directly for deterministic tests.

    The real ``get_env_value`` defers to ``hermes_cli.config`` which may pull
    values from ``~/.hermes/.env``; for tests we want behaviour tied strictly to
    the environment that ``monkeypatch`` controls.
    """

    def _gev(name, default=None):
        return os.getenv(name, default)

    monkeypatch.setattr(tts_tool, "get_env_value", _gev)
    return _gev


# ---------------------------------------------------------------------------
# Registration / constants
# ---------------------------------------------------------------------------

class TestTelnyxRegistration:
    def test_telnyx_is_a_builtin_provider(self):
        assert "telnyx" in BUILTIN_TTS_PROVIDERS

    def test_telnyx_has_a_text_length_cap(self):
        assert "telnyx" in PROVIDER_MAX_TEXT_LENGTH
        assert PROVIDER_MAX_TEXT_LENGTH["telnyx"] == 5000


class TestTelnyxConstants:
    def test_default_base_url(self):
        assert TELNYX_TTS_DEFAULT_BASE_URL == "wss://api.telnyx.com/v2/text-to-speech"

    def test_default_voice(self):
        assert DEFAULT_TELNYX_VOICE == "Telnyx.NaturalHD.astra"

    def test_voice_families_declared(self):
        assert "Telnyx.NaturalHD" in TELNYX_TTS_VOICE_FAMILIES
        assert "Telnyx.KokoroTTS" in TELNYX_TTS_VOICE_FAMILIES

    def test_fallback_voices_declared(self):
        assert "Telnyx.NaturalHD.astra" in TELNYX_FALLBACK_VOICES
        assert "Telnyx.KokoroTTS.af_alloy" in TELNYX_FALLBACK_VOICES
        assert len(TELNYX_FALLBACK_VOICES) >= 4


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

class TestTelnyxSignature:
    def test_function_exists(self):
        assert callable(_generate_telnyx_tts)

    def test_signature(self):
        sig = inspect.signature(_generate_telnyx_tts)
        params = list(sig.parameters)
        assert params == ["text", "output_path", "tts_config"]
        assert sig.return_annotation is str

    def test_protocol_frames_present_in_source(self):
        """All three required frames must appear in the implementation."""
        import inspect as _inspect
        source = _inspect.getsource(_generate_telnyx_tts)
        assert "/speech?voice=" in source
        # Init frame (single space)
        assert '"text": " "' in source
        # Text frame
        assert '"text": text' in source
        # Stop frame (empty text)
        assert '"text": ""' in source


# ---------------------------------------------------------------------------
# WebSocket protocol / runtime
# ---------------------------------------------------------------------------

class TestGenerateTelnyxTts:
    def test_writes_audio(self, tmp_path, monkeypatch, env_api_key):
        """Happy path: receives two audio chunks, isFinal on the second."""
        messages = [
            json.dumps({"audio": base64.b64encode(FAKE_CHUNK_1).decode()}),
            json.dumps({"audio": base64.b64encode(FAKE_CHUNK_2).decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key-xyz")

        out = tmp_path / "tts_out.mp3"
        result = _generate_telnyx_tts("Hello world", str(out), {})

        assert result == str(out)
        assert out.exists()
        assert out.read_bytes() == FAKE_CHUNK_1 + FAKE_CHUNK_2

    def test_sends_three_frames_in_order(self, tmp_path, monkeypatch, env_api_key):
        """The function must send exactly init, text, and stop frames."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"DATA").decode(), "isFinal": True}),
        ]
        fake_ws_module, ws_instance = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Test text", str(out), {})

        assert len(ws_instance.sent) == 3
        connect_url = fake_ws_module.connect.call_args.args[0]
        assert connect_url == (
            "wss://api.telnyx.com/v2/text-to-speech/speech"
            "?voice=Telnyx.NaturalHD.astra"
        )

        init_frame = json.loads(ws_instance.sent[0])
        assert init_frame["text"] == " "
        assert "voice" not in init_frame
        assert "output_format" not in init_frame

        text_frame = json.loads(ws_instance.sent[1])
        assert text_frame["text"] == "Test text"

        stop_frame = json.loads(ws_instance.sent[2])
        assert stop_frame["text"] == ""

    def test_uses_custom_voice(self, tmp_path, monkeypatch, env_api_key):
        """Voice from tts_config must appear in the connect URL."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"X").decode(), "isFinal": True}),
        ]
        fake_ws_module, ws_instance = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts(
            "Hello", str(out), {"telnyx": {"voice": "Telnyx.KokoroTTS.af_bella"}}
        )

        init_frame = json.loads(ws_instance.sent[0])
        assert init_frame == {"text": " "}
        connect_url = fake_ws_module.connect.call_args.args[0]
        assert connect_url == (
            "wss://api.telnyx.com/v2/text-to-speech/speech"
            "?voice=Telnyx.KokoroTTS.af_bella"
        )

    def test_uses_custom_base_url(self, tmp_path, monkeypatch, env_api_key):
        """base_url override from tts_config must be honoured."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"X").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts(
            "Hello", str(out),
            {"telnyx": {"base_url": "wss://custom.example.com/tts"}},
        )

        connect_url = fake_ws_module.connect.call_args.args[0]
        assert connect_url == "wss://custom.example.com/tts/speech?voice=Telnyx.NaturalHD.astra"

    def test_uses_env_base_url(self, tmp_path, monkeypatch, env_api_key):
        """TELNYX_TTS_BASE_URL env var overrides the default base URL."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"X").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")
        monkeypatch.setenv("TELNYX_TTS_BASE_URL", "wss://env.example.com/v2")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Hello", str(out), {})

        connect_url = fake_ws_module.connect.call_args.args[0]
        assert connect_url == "wss://env.example.com/v2/speech?voice=Telnyx.NaturalHD.astra"

    def test_skips_invalid_json(self, tmp_path, monkeypatch, env_api_key):
        """Non-JSON messages must be silently skipped."""
        messages = [
            "not-json",
            json.dumps({"audio": base64.b64encode(b"VALID").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Hello", str(out), {})
        assert out.read_bytes() == b"VALID"

    def test_accepts_audio_field_aliases(self, tmp_path, monkeypatch, env_api_key):
        """``data`` and ``audio_base64`` field aliases must be accepted."""
        messages = [
            json.dumps({"data": base64.b64encode(b"A").decode()}),
            json.dumps({"audio_base64": base64.b64encode(b"B").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Hello", str(out), {})
        assert out.read_bytes() == b"AB"

    def test_stops_on_is_final_snake_case(self, tmp_path, monkeypatch, env_api_key):
        """``is_final`` (snake_case) must also terminate the stream."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"OK").decode(), "is_final": True}),
            # Would corrupt output if the loop didn't stop:
            json.dumps({"audio": base64.b64encode(b"LEAK").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Hello", str(out), {})
        assert out.read_bytes() == b"OK"

    def test_empty_stream_raises_runtime_error(self, tmp_path, monkeypatch, env_api_key):
        """A stream that returns no audio chunks must raise RuntimeError."""
        messages = [json.dumps({"isFinal": True})]  # no audio field
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        with pytest.raises(RuntimeError, match="no audio chunks"):
            _generate_telnyx_tts("Hello", str(tmp_path / "out.mp3"), {})

    def test_sends_authorization_bearer_header(self, tmp_path, monkeypatch, env_api_key):
        """The connect call must carry an Authorization: Bearer header."""
        messages = [
            json.dumps({"audio": base64.b64encode(b"X").decode(), "isFinal": True}),
        ]
        fake_ws_module, _ws = _make_fake_websockets(messages)
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "super-secret")

        out = tmp_path / "out.mp3"
        _generate_telnyx_tts("Hello", str(out), {})

        headers = fake_ws_module.connect.call_args.kwargs.get("additional_headers")
        assert headers == {"Authorization": "Bearer super-secret"}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestTelnyxErrors:
    def test_missing_api_key_raises_value_error(self, tmp_path, monkeypatch, env_api_key):
        """Missing TELNYX_API_KEY must raise ValueError."""
        fake_ws_module, _ws = _make_fake_websockets([])
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.delenv("TELNYX_API_KEY", raising=False)

        with pytest.raises(ValueError, match="TELNYX_API_KEY"):
            _generate_telnyx_tts("Hello", str(tmp_path / "out.mp3"), {})

    def test_blank_api_key_raises_value_error(self, tmp_path, monkeypatch, env_api_key):
        """A whitespace-only API key must be treated as missing."""
        fake_ws_module, _ws = _make_fake_websockets([])
        monkeypatch.setitem(sys.modules, "websockets", fake_ws_module)
        monkeypatch.setenv("TELNYX_API_KEY", "   ")

        with pytest.raises(ValueError, match="TELNYX_API_KEY"):
            _generate_telnyx_tts("Hello", str(tmp_path / "out.mp3"), {})

    def test_missing_websockets_raises_import_error(self, tmp_path, monkeypatch, env_api_key):
        """Missing websockets package must raise ImportError with an install hint."""
        # ``None`` in sys.modules makes ``import websockets`` raise ImportError.
        monkeypatch.setitem(sys.modules, "websockets", None)  # type: ignore[arg-type]
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        with pytest.raises(ImportError, match="websockets"):
            _generate_telnyx_tts("Hello", str(tmp_path / "out.mp3"), {})

    def test_missing_websockets_error_mentions_pip_install(self, tmp_path, monkeypatch, env_api_key):
        """The ImportError message must include a ``pip install`` hint."""
        monkeypatch.setitem(sys.modules, "websockets", None)  # type: ignore[arg-type]
        monkeypatch.setenv("TELNYX_API_KEY", "test-key")

        with pytest.raises(ImportError, match="pip install websockets"):
            _generate_telnyx_tts("Hello", str(tmp_path / "out.mp3"), {})


# ---------------------------------------------------------------------------
# Dispatch through text_to_speech_tool
# ---------------------------------------------------------------------------

class TestTelnyxDispatch:
    def test_dispatch_routes_to_generate_telnyx(self, monkeypatch, tmp_path):
        """provider=telnyx must route to _generate_telnyx_tts and report success."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"tts": {"provider": "telnyx"}})
        )

        calls = []

        def _fake_generate(text, output_path, tts_config):
            calls.append((text, output_path, tts_config))
            # Emulate the provider writing an MP3 so the output-file check passes.
            import pathlib
            pathlib.Path(output_path).write_bytes(b"ID3fake-mp3-bytes")
            return output_path

        monkeypatch.setattr(tts_tool, "_generate_telnyx_tts", _fake_generate)

        from tools.tts_tool import text_to_speech_tool

        result = json.loads(text_to_speech_tool(text="Hello from Telnyx"))

        assert result["success"] is True
        assert result["provider"] == "telnyx"
        assert len(calls) == 1
        assert calls[0][0] == "Hello from Telnyx"
        # The telnyx config block (empty here) is forwarded as the third arg.
        assert isinstance(calls[0][2], dict)

    def test_dispatch_returns_mp3_path(self, monkeypatch, tmp_path):
        """Without a native-opus provider, the saved file must be .mp3."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"tts": {"provider": "telnyx"}})
        )

        def _fake_generate(text, output_path, tts_config):
            import pathlib
            pathlib.Path(output_path).write_bytes(b"ID3fake-mp3-bytes")
            return output_path

        monkeypatch.setattr(tts_tool, "_generate_telnyx_tts", _fake_generate)

        from tools.tts_tool import text_to_speech_tool

        result = json.loads(text_to_speech_tool(text="Hi"))
        assert result["success"] is True
        assert result["file_path"].endswith(".mp3")

    def test_dispatch_surfaces_missing_api_key_error(self, monkeypatch, tmp_path):
        """A ValueError from _generate_telnyx_tts must surface as a JSON error."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"tts": {"provider": "telnyx"}})
        )

        def _raise_value_error(text, output_path, tts_config):
            raise ValueError("TELNYX_API_KEY is not set.")

        monkeypatch.setattr(tts_tool, "_generate_telnyx_tts", _raise_value_error)

        from tools.tts_tool import text_to_speech_tool

        result = json.loads(text_to_speech_tool(text="Hi"))
        assert result["success"] is False
        assert "TELNYX_API_KEY" in result["error"]
