"""
Tests for the native VoxCPM TTS provider.

VoxCPM (https://github.com/OpenBMB/VoxCPM) is OpenBMB's tokenizer-free
diffusion-autoregressive TTS engine. VoxCPM2 (default) is a 2B parameter
model supporting 30 languages, voice design from text prompts, and
reference-audio voice cloning.

These tests pin the registry / config resolution / dispatch / error paths
WITHOUT requiring the 5 GB model weights or the ``voxcpm`` Python package
to be installed in CI. Synthesis is monkey-patched to return a fake
numpy waveform. The optional ``test_voxcpm_e2e_*`` tests are skipped
unless the user has installed ``voxcpm`` AND placed the model weights
under ``~/.hermes/models/VoxCPM2`` (or the path they configure).
"""

import json
import os
import sys
import types
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import tts_tool
from tools.tts_tool import (
    BUILTIN_TTS_PROVIDERS,
    DEFAULT_VOXCPM_CFG_VALUE,
    DEFAULT_VOXCPM_INFERENCE_TIMESTEPS,
    DEFAULT_VOXCPM_MODEL,
    PROVIDER_MAX_TEXT_LENGTH,
    _check_voxcpm_available,
    _generate_voxcpm_tts,
    _import_voxcpm,
    _resolve_voxcpm_model_path,
)


# ---------------------------------------------------------------------------
# Helpers — fake VoxCPM for unit tests (no model load)
# ---------------------------------------------------------------------------


class _FakeVoxCPM:
    """Minimal stand-in for voxcpm.VoxCPM used by the unit tests.

    Records every generate() call so tests can assert on the kwargs we
    passed and returns a deterministic 1-second silence waveform at the
    model-declared sample rate.
    """

    # Class-level call recorders (reset by _patch_voxcpm).
    # Initialized to None / 0 so tests can detect "no call yet" without
    # relying on attribute creation order.
    last_call_kwargs = None
    last_call_count = 0

    def __init__(self):
        self.calls = []
        self.tts_model = MagicMock()
        self.tts_model.sample_rate = 48000  # VoxCPM2 native rate

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        return cls()

    def generate(self, **kwargs):
        type(self).last_call_kwargs = kwargs
        type(self).last_call_count += 1
        import numpy as np
        return np.zeros(self.tts_model.sample_rate, dtype=np.float32)


def _patch_voxcpm(monkeypatch):
    """Wire up the fake VoxCPM class so _import_voxcpm() returns it."""
    monkeypatch.setattr(tts_tool, "_import_voxcpm", lambda: _FakeVoxCPM)
    # Reset call recorders so each test starts clean
    _FakeVoxCPM.last_call_kwargs = None
    _FakeVoxCPM.last_call_count = 0


@pytest.fixture(autouse=True)
def _fake_soundfile(monkeypatch):
    """Stub the optional `soundfile` dependency so the unit tests run in CI.

    `_generate_voxcpm_tts` does `import soundfile as sf` at runtime and
    calls `sf.write(path, audio, sample_rate, subtype=...)`. CI does not
    install soundfile (voxcpm is optional), so without this stub every
    config-resolution test fails with ModuleNotFoundError. The stub writes
    a real, wave-module-readable WAV so the output assertions still hold.
    """
    import types
    import wave

    fake_sf = types.ModuleType("soundfile")

    def _fake_write(path, audio, sample_rate, subtype="PCM_16", **kwargs):
        import numpy as np
        samples = np.asarray(audio)
        if samples.dtype != np.int16:
            # Normalize float audio to int16 so `wave` can write it.
            scaled = np.clip(samples, -1.0, 1.0)
            samples = (scaled * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())

    fake_sf.write = _fake_write
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    yield fake_sf


# ---------------------------------------------------------------------------
# Registry / constants
# ---------------------------------------------------------------------------


class TestVoxCPMRegistration:
    def test_voxcpm_is_a_builtin_provider(self):
        assert "voxcpm" in BUILTIN_TTS_PROVIDERS

    def test_voxcpm_has_a_text_length_cap(self):
        cap = PROVIDER_MAX_TEXT_LENGTH.get("voxcpm", 0)
        assert cap > 0, "VoxCPM provider must declare a max_text_length"

    def test_default_model_is_voxcpm2(self):
        assert DEFAULT_VOXCPM_MODEL == "openbmb/VoxCPM2"

    def test_default_cfg_value_in_safe_range(self):
        # VoxCPM's own docs recommend 1.0-3.0; we land at 2.0 by default
        assert 1.0 <= DEFAULT_VOXCPM_CFG_VALUE <= 3.0

    def test_default_inference_timesteps_reasonable(self):
        # VoxCPM's docs recommend 4-30; default 10 balances speed/quality
        assert 4 <= DEFAULT_VOXCPM_INFERENCE_TIMESTEPS <= 30


# ---------------------------------------------------------------------------
# _check_voxcpm_available
# ---------------------------------------------------------------------------


class TestCheckVoxCPMAvailable:
    def test_returns_bool_without_raising(self):
        # Probe must never raise — env-dependent answer is fine.
        assert isinstance(_check_voxcpm_available(), bool)

    def test_returns_false_when_voxcpm_missing(self, monkeypatch):
        import importlib.util
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name: None if name == "voxcpm" else importlib.util.find_spec(name),
        )
        assert _check_voxcpm_available() is False


# ---------------------------------------------------------------------------
# _resolve_voxcpm_model_path
# ---------------------------------------------------------------------------


class TestResolveVoxCPMModelPath:
    def test_local_directory_returned_expanded(self, tmp_path):
        # Existing directory is returned as-is (after ~ expansion)
        home_dir = tmp_path / "fakehome" / "models" / "VoxCPM2"
        home_dir.mkdir(parents=True)
        resolved = _resolve_voxcpm_model_path(str(home_dir))
        assert resolved == str(home_dir)

    def test_repo_id_passes_through(self):
        # Repo ids ("org/name") are returned unchanged so voxcpm can resolve
        repo = "openbmb/VoxCPM2"
        assert _resolve_voxcpm_model_path(repo) == repo

    def test_expands_user_when_path_does_not_exist(self, monkeypatch):
        # If the user wrote ~/models/voxcpm but the path doesn't exist we
        # still want ~ expanded (and the model loader will surface a clear
        # FileNotFoundError later — we don't pretend it exists here).
        monkeypatch.setenv("HOME", "/tmp/fake-home")
        result = _resolve_voxcpm_model_path("~/models/VoxCPM2")
        assert result == os.path.expanduser("~/models/VoxCPM2")


# ---------------------------------------------------------------------------
# _generate_voxcpm_tts — config / dispatch (no model load)
# ---------------------------------------------------------------------------


class TestGenerateVoxCPMConfigResolution:
    """Verify that _generate_voxcpm_tts reads config keys and calls
    generate() with the expected kwargs, without needing a real model."""

    def test_uses_defaults_when_no_voxcpm_config(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        out = tmp_path / "test.wav"
        _generate_voxcpm_tts("Hello world", str(out), tts_config={})

        kwargs = _FakeVoxCPM.last_call_kwargs
        assert kwargs is not None
        assert kwargs["text"] == "Hello world"
        assert kwargs["cfg_value"] == DEFAULT_VOXCPM_CFG_VALUE
        assert kwargs["inference_timesteps"] == DEFAULT_VOXCPM_INFERENCE_TIMESTEPS
        assert kwargs["normalize"] is False
        # No reference / prompt paths when not configured
        assert "reference_wav_path" not in kwargs
        assert "prompt_wav_path" not in kwargs

    def test_passes_custom_cfg_and_steps(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        cfg = {
            "voxcpm": {
                "cfg_value": 1.5,
                "inference_timesteps": 20,
                "normalize": True,
            }
        }
        _generate_voxcpm_tts("Test", str(tmp_path / "test.wav"), cfg)
        kwargs = _FakeVoxCPM.last_call_kwargs
        assert kwargs is not None
        assert kwargs["cfg_value"] == 1.5
        assert kwargs["inference_timesteps"] == 20
        assert kwargs["normalize"] is True

    def test_reference_wav_path_passed_through(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"fake wav")
        cfg = {"voxcpm": {"reference_wav_path": str(ref)}}
        _generate_voxcpm_tts("Clone me", str(tmp_path / "out.wav"), cfg)
        kwargs = _FakeVoxCPM.last_call_kwargs
        assert kwargs is not None
        assert kwargs["reference_wav_path"] == str(ref)

    def test_prompt_wav_and_text_passed_together(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        prompt = tmp_path / "prompt.wav"
        prompt.write_bytes(b"fake wav")
        cfg = {
            "voxcpm": {
                "prompt_wav_path": str(prompt),
                "prompt_text": "This is the reference transcript.",
            }
        }
        _generate_voxcpm_tts("Continue me", str(tmp_path / "out.wav"), cfg)
        kwargs = _FakeVoxCPM.last_call_kwargs
        assert kwargs is not None
        assert kwargs["prompt_wav_path"] == str(prompt)
        assert kwargs["prompt_text"] == "This is the reference transcript."

    def test_prompt_wav_without_text_raises(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        prompt = tmp_path / "prompt.wav"
        prompt.write_bytes(b"fake wav")
        cfg = {"voxcpm": {"prompt_wav_path": str(prompt), "prompt_text": ""}}
        # prompt_text empty string is falsy → treated as None → mismatch
        with pytest.raises(ValueError, match="prompt_wav_path and prompt_text"):
            _generate_voxcpm_tts(
                "x", str(tmp_path / "out.wav"), cfg,
            )

    def test_missing_reference_wav_raises(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        cfg = {"voxcpm": {"reference_wav_path": "/nonexistent/ref.wav"}}
        with pytest.raises(FileNotFoundError, match="reference_wav_path"):
            _generate_voxcpm_tts(
                "x", str(tmp_path / "out.wav"), cfg,
            )


# ---------------------------------------------------------------------------
# _generate_voxcpm_tts — WAV file output (no model load)
# ---------------------------------------------------------------------------


class TestGenerateVoxCPMWAVOutput:
    """Verify that the synthesized audio is written as a real WAV the
    caller can read back. No model load — only a fake numpy array."""

    def test_writes_a_real_wav_file(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        out = tmp_path / "output.wav"
        _generate_voxcpm_tts(
            "Hello VoxCPM", str(out), tts_config={},
        )
        assert out.exists()
        assert out.stat().st_size > 0
        # Wave module can open what we wrote
        with wave.open(str(out), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 48000  # VoxCPM2 native rate

    def test_converts_to_mp3_when_output_path_is_mp3(self, tmp_path, monkeypatch):
        _patch_voxcpm(monkeypatch)
        # Skip this test if ffmpeg isn't installed — the function falls
        # back to keeping the WAV under that name, which the caller
        # contract still accepts.
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            pytest.skip("ffmpeg not installed; conversion branch untestable")

        out = tmp_path / "output.mp3"
        _generate_voxcpm_tts("Hello", str(out), tts_config={})
        assert out.exists()
        assert out.stat().st_size > 0
        # ffmpeg writes mp3 with either ID3 or MPEG frame sync header
        head = out.read_bytes()[:4]
        assert head[:3] == b"ID3" or head[:2] == b"\xff\xfb"


def _find_ffmpeg():
    """Locate ffmpeg on PATH (mirrors shutil.which without importing shutil)."""
    import shutil
    return shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# _import_voxcpm — lazy import contract
# ---------------------------------------------------------------------------


class TestImportVoxCPM:
    def test_returns_callable_class(self, monkeypatch):
        """When voxcpm is importable, _import_voxcpm returns a callable class."""
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "voxcpm":
                return types.SimpleNamespace(VoxCPM=_FakeVoxCPM)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        cls = _import_voxcpm()
        assert callable(cls)

    def test_raises_import_error_when_missing(self, monkeypatch):
        # Simulate "not installed" by hiding the module from importlib.util.
        import importlib.util
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "voxcpm" or name.startswith("voxcpm."):
                raise ImportError(f"No module named '{name}' (simulated)")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        # Now _import_voxcpm's hard-coded "from voxcpm import VoxCPM" must
        # raise ImportError (the lazy-import guard surfaces it to the caller).
        with pytest.raises(ImportError):
            _import_voxcpm()


# ---------------------------------------------------------------------------
# E2E test — runs the real VoxCPM2 if weights + package are present
# ---------------------------------------------------------------------------
#
# This test is skipped by default. To run it locally:
#
#   pip install voxcpm
#   python -c "from modelscope import snapshot_download; snapshot_download('OpenBMB/VoxCPM2', local_dir='~/.hermes/models/VoxCPM2')"
#   pytest tests/tools/test_tts_voxcpm.py::TestVoxCPMEnd2End -v
#
# Generates a real 48 kHz WAV and asserts it's a non-trivial waveform
# (not silence). Uses MPS on Apple Silicon by default.


@pytest.mark.skipif(
    not _check_voxcpm_available(),
    reason="voxcpm Python package not installed",
)
class TestVoxCPMEnd2End:
    def test_real_voxcpm2_voice_design(self, tmp_path):
        model_dir = Path(os.path.expanduser("~/.hermes/models/VoxCPM2"))
        if not model_dir.exists():
            pytest.skip(
                "VoxCPM2 weights not present at ~/.hermes/models/VoxCPM2 "
                "(download via ModelScope to run this E2E test)"
            )

        out = tmp_path / "voxcpm_e2e.wav"
        _generate_voxcpm_tts(
            "VoxCPM2 end-to-end test on Apple Silicon.",
            str(out),
            tts_config={
                "voxcpm": {
                    "model": str(model_dir),
                    "device": "auto",
                    "local_files_only": True,
                    "cfg_value": 2.0,
                    "inference_timesteps": 10,
                }
            },
        )
        assert out.exists()
        assert out.stat().st_size > 48000 * 2  # at least 1 second of 16-bit audio

        # Open the WAV back and confirm it decodes to a non-silent signal
        import numpy as np
        with wave.open(str(out), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16)
            sr = wav.getframerate()
        assert sr == 48000, f"VoxCPM2 expected 48kHz, got {sr}"
        assert audio.dtype == np.int16
        # Real speech has audible amplitude — silence would be all zeros.
        # Threshold is intentionally loose so we don't depend on test text.
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        assert rms > 50, f"Generated audio looks like silence (rms={rms})"