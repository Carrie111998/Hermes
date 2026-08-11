"""
Tests for the native Kokoro TTS provider.

These tests pin the resolution / caching / dispatch paths for Kokoro
without requiring the ``kokoro`` package (or torch) to actually be
installed — the pipeline is monkey-patched to avoid the torch/model load.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import tts_tool
from tools.tts_tool import (
    BUILTIN_TTS_PROVIDERS,
    DEFAULT_KOKORO_LANG_CODE,
    DEFAULT_KOKORO_VOICE,
    PROVIDER_MAX_TEXT_LENGTH,
    _check_kokoro_available,
    check_tts_requirements,
    text_to_speech_tool,
)


# ---------------------------------------------------------------------------
# Registry / constants
# ---------------------------------------------------------------------------

class TestKokoroRegistration:
    def test_kokoro_is_a_builtin_provider(self):
        assert "kokoro" in BUILTIN_TTS_PROVIDERS

    def test_kokoro_has_a_text_length_cap(self):
        assert PROVIDER_MAX_TEXT_LENGTH.get("kokoro", 0) > 0


# ---------------------------------------------------------------------------
# _check_kokoro_available
# ---------------------------------------------------------------------------

class TestCheckKokoroAvailable:
    def test_returns_bool_without_raising(self):
        # We don't care about the current environment's answer — just that
        # the probe never raises on a machine without kokoro installed.
        assert isinstance(_check_kokoro_available(), bool)


# ---------------------------------------------------------------------------
# _generate_kokoro_tts — stubbed so we don't need kokoro/torch installed
# ---------------------------------------------------------------------------

class _StubResult:
    """Stand-in for KPipeline.Result: holds an audio array (plain list)."""

    def __init__(self, audio):
        self.audio = audio


class _StubKPipeline:
    """Stand-in for kokoro.KPipeline used by the synthesis tests."""

    instances: list[tuple] = []
    calls: list[tuple] = []

    def __init__(self, lang_code, device=None):
        _StubKPipeline.instances.append((lang_code, device))

    def __call__(self, text, voice=None, speed=1, **kwargs):
        _StubKPipeline.calls.append((text, voice, speed))
        audio = [0.0] * 2400  # 0.1s at 24kHz
        return iter([_StubResult(audio)])


@pytest.fixture(autouse=True)
def _reset_kokoro_cache():
    """Clear the module-level pipeline cache between tests."""
    tts_tool._kokoro_pipeline_cache.clear()
    _StubKPipeline.instances = []
    _StubKPipeline.calls = []
    yield
    tts_tool._kokoro_pipeline_cache.clear()


@pytest.fixture
def _stub_soundfile():
    """Stub soundfile — the real package isn't installed in the CI venv, and
    _generate_kokoro_tts does `import soundfile as sf` at runtime."""
    fake_sf = MagicMock()

    def _fake_write(path, audio, samplerate):
        # Emulate writing a real file so downstream path checks succeed.
        Path(path).write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt fake")

    fake_sf.write = _fake_write
    with patch.dict("sys.modules", {"soundfile": fake_sf}):
        yield fake_sf


class TestGenerateKokoroTts:
    def test_synthesizes_wav_with_configured_voice(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        out_path = str(tmp_path / "out.wav")
        config = {"kokoro": {"voice": "am_adam", "lang_code": "a"}}

        result = tts_tool._generate_kokoro_tts("hello", out_path, config)

        assert result == out_path
        assert Path(out_path).exists()
        assert Path(out_path).stat().st_size > 0
        assert _StubKPipeline.instances == [("a", None)]
        assert _StubKPipeline.calls == [("hello", "am_adam", 1.0)]

    def test_uses_defaults_when_unconfigured(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        result = tts_tool._generate_kokoro_tts("hi", str(tmp_path / "out.wav"), {})

        assert _StubKPipeline.instances == [(DEFAULT_KOKORO_LANG_CODE, None)]
        assert _StubKPipeline.calls == [("hi", DEFAULT_KOKORO_VOICE, 1.0)]

    def test_pipeline_cached_across_calls(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        config = {"kokoro": {"voice": "af_heart", "lang_code": "a"}}
        tts_tool._generate_kokoro_tts("one", str(tmp_path / "a.wav"), config)
        tts_tool._generate_kokoro_tts("two", str(tmp_path / "b.wav"), config)

        # One pipeline construction (model load) for both calls.
        assert _StubKPipeline.instances == [("a", None)]
        assert [c[0] for c in _StubKPipeline.calls] == ["one", "two"]

    def test_lang_change_loads_separate_pipeline(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        tts_tool._generate_kokoro_tts("hello", str(tmp_path / "a.wav"), {"kokoro": {"lang_code": "a"}})
        tts_tool._generate_kokoro_tts("hola", str(tmp_path / "b.wav"), {"kokoro": {"lang_code": "e"}})

        assert _StubKPipeline.instances == [("a", None), ("e", None)]

    def test_device_knob_forwarded(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        config = {"kokoro": {"device": "cpu"}}
        tts_tool._generate_kokoro_tts("hi", str(tmp_path / "out.wav"), config)

        assert _StubKPipeline.instances == [("a", "cpu")]

    def test_empty_audio_raises(self, tmp_path, monkeypatch, _stub_soundfile):
        class _EmptyPipeline(_StubKPipeline):
            def __call__(self, text, voice=None, speed=1, **kwargs):
                return iter([])

        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _EmptyPipeline)

        with pytest.raises(RuntimeError, match="no audio"):
            tts_tool._generate_kokoro_tts("hi", str(tmp_path / "out.wav"), {"kokoro": {}})


# ---------------------------------------------------------------------------
# text_to_speech_tool end-to-end (provider == "kokoro")
# ---------------------------------------------------------------------------

class TestTextToSpeechToolWithKokoro:
    def test_dispatches_to_kokoro(self, tmp_path, monkeypatch, _stub_soundfile):
        monkeypatch.setattr(tts_tool, "_import_kokoro", lambda: _StubKPipeline)

        cfg = {"provider": "kokoro", "kokoro": {"voice": "af_heart"}}
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: cfg)

        result = text_to_speech_tool(text="hi", output_path=str(tmp_path / "clip.wav"))
        data = json.loads(result)

        assert data["success"] is True, data
        assert data["provider"] == "kokoro"
        assert Path(data["file_path"]).exists()

    def test_missing_package_surfaces_error(self, tmp_path, monkeypatch):
        def raise_import():
            raise ImportError("No module named 'kokoro'")

        monkeypatch.setattr(tts_tool, "_import_kokoro", raise_import)

        cfg = {"provider": "kokoro"}
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: cfg)

        result = text_to_speech_tool(text="hi", output_path=str(tmp_path / "clip.wav"))
        data = json.loads(result)

        assert data["success"] is False
        assert "kokoro" in data["error"]


# ---------------------------------------------------------------------------
# check_tts_requirements
# ---------------------------------------------------------------------------

class TestCheckTtsRequirementsKokoro:
    def test_kokoro_install_satisfies_requirements(self, monkeypatch):
        # Drop every other provider so we can isolate the kokoro signal.
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "kokoro"})
        monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: (_ for _ in ()).throw(ImportError()))
        monkeypatch.setattr(tts_tool, "_import_elevenlabs", lambda: (_ for _ in ()).throw(ImportError()))
        monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: (_ for _ in ()).throw(ImportError()))
        monkeypatch.setattr(tts_tool, "_import_mistral_client", lambda: (_ for _ in ()).throw(ImportError()))
        monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: False)
        monkeypatch.setattr(tts_tool, "_check_kittentts_available", lambda: False)
        monkeypatch.setattr(tts_tool, "_check_piper_available", lambda: False)
        monkeypatch.setattr(tts_tool, "_has_any_command_tts_provider", lambda: False)
        monkeypatch.setattr(tts_tool, "_has_openai_audio_backend", lambda: False)
        for env in ("MINIMAX_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY",
                    "GOOGLE_API_KEY", "MISTRAL_API_KEY", "ELEVENLABS_API_KEY"):
            monkeypatch.delenv(env, raising=False)

        # Now toggle the kokoro check on and off.
        monkeypatch.setattr(tts_tool, "_check_kokoro_available", lambda: False)
        assert check_tts_requirements() is False

        monkeypatch.setattr(tts_tool, "_check_kokoro_available", lambda: True)
        assert check_tts_requirements() is True
