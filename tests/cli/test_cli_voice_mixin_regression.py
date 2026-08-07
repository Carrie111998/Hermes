"""Regression tests for the CLIVoiceMixin extraction.

God-file decomposition Wave 1 (cli.py shard s4, cluster c6): the voice-mode
methods moved verbatim from ``cli.py``'s ``HermesCLI`` into
``hermes_cli/cli_voice_mixin.py``. ``HermesCLI`` now inherits
``CLIVoiceMixin``, so the behavior is identical via the MRO.

These tests exercise the mixin through a bare stub host and stub the ``cli``
module so the lazy ``from cli import ...`` lines resolve without importing the
full CLI (same isolation trick as ``tests/cli/test_cli_extension_hooks.py``).
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.cli_voice_mixin import CLIVoiceMixin


class _Stub(CLIVoiceMixin):
    """Bare mixin host: each test sets only the attributes it exercises."""


class _VoiceInputMessage:
    """Mirror of cli._VoiceInputMessage used by the stubbed cli module."""

    def __init__(self, text: str):
        self.text = text


@pytest.fixture(autouse=True)
def _cli_stub():
    """Stub the cli module so lazy imports inside moved methods resolve."""
    cli = MagicMock()
    cli._cprint = lambda *a, **k: None
    cli._DIM = ""
    cli._RST = ""
    cli._ACCENT = ""
    cli._BOLD = ""
    cli.logger = MagicMock()
    cli._VoiceInputMessage = _VoiceInputMessage
    with patch.dict(sys.modules, {"cli": cli}):
        yield


def _lock() -> threading.Lock:
    return threading.Lock()


# --------------------------------------------------------------------------
# STT config resolution (pure, config-parsing)
# --------------------------------------------------------------------------

def test_stt_model_local_defaults_to_base():
    stub = _Stub()
    with patch("hermes_cli.config.load_config", return_value={"stt": {"provider": "local"}}):
        assert stub._voice_stt_model() == "base"


def test_stt_model_local_explicit_model():
    stub = _Stub()
    with patch(
        "hermes_cli.config.load_config",
        return_value={"stt": {"provider": "local", "local": {"model": "tiny"}}},
    ):
        assert stub._voice_stt_model() == "tiny"


def test_stt_model_remote_provider_model():
    stub = _Stub()
    with patch(
        "hermes_cli.config.load_config",
        return_value={"stt": {"provider": "groq", "model": "whisper-large-v3"}},
    ):
        assert stub._voice_stt_model() == "whisper-large-v3"


def test_stt_model_malformed_config_returns_none():
    stub = _Stub()
    with patch("hermes_cli.config.load_config", return_value={"stt": "not-a-dict"}):
        assert stub._voice_stt_model() is None
    with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
        assert stub._voice_stt_model() is None


def test_stt_provider_lowercased():
    stub = _Stub()
    with patch("hermes_cli.config.load_config", return_value={"stt": {"provider": "GROQ"}}):
        assert stub._voice_stt_provider() == "groq"


def test_stt_provider_missing_or_malformed():
    stub = _Stub()
    with patch("hermes_cli.config.load_config", return_value={}):
        assert stub._voice_stt_provider() == ""
    with patch("hermes_cli.config.load_config", return_value={"stt": []}):
        assert stub._voice_stt_provider() == ""


# --------------------------------------------------------------------------
# beep preference (config parsing with is_truthy_value semantics)
# --------------------------------------------------------------------------

def test_beeps_enabled_quoted_false_is_false():
    stub = _Stub()
    with patch(
        "hermes_cli.config.load_config",
        return_value={"voice": {"beep_enabled": "false"}},
    ):
        assert stub._voice_beeps_enabled() is False


def test_beeps_enabled_default_true():
    stub = _Stub()
    with patch("hermes_cli.config.load_config", return_value={}):
        assert stub._voice_beeps_enabled() is True
    with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
        assert stub._voice_beeps_enabled() is True


# --------------------------------------------------------------------------
# _typed_voice_stop — typed stop-phrase gating
# --------------------------------------------------------------------------

def test_typed_voice_stop_non_string_passthrough():
    stub = _Stub()
    stub._voice_lock = _lock()
    stub._voice_mode = True
    stub._voice_continuous = False
    assert stub._typed_voice_stop(42) is False


def test_typed_voice_stop_requires_voice_mode():
    stub = _Stub()
    stub._voice_lock = _lock()
    stub._voice_mode = False
    stub._voice_continuous = False
    assert stub._typed_voice_stop("stop") is False


def test_typed_voice_stop_accepts_stop_phrase():
    stub = _Stub()
    stub._voice_lock = _lock()
    stub._voice_mode = True
    stub._voice_continuous = False
    stub._disable_voice_mode = MagicMock()
    with patch("tools.voice_mode.is_voice_stop_phrase", return_value=True):
        assert stub._typed_voice_stop("stop") is True
    stub._disable_voice_mode.assert_called_once()


def test_typed_voice_stop_non_stop_phrase_passthrough():
    stub = _Stub()
    stub._voice_lock = _lock()
    stub._voice_mode = True
    stub._voice_continuous = False
    stub._disable_voice_mode = MagicMock()
    with patch("tools.voice_mode.is_voice_stop_phrase", return_value=False):
        assert stub._typed_voice_stop("hello") is False
    stub._disable_voice_mode.assert_not_called()


# --------------------------------------------------------------------------
# _voice_submit_barge_utterance — transcript queueing / restart fallback
# --------------------------------------------------------------------------

def _barge_stub(tmp_path):
    stub = _Stub()
    stub._voice_lock = _lock()
    stub._voice_barge_capture = MagicMock()
    stub._pending_input = MagicMock()
    stub._voice_restart_recording_async = MagicMock()
    stub._disable_voice_mode = MagicMock()
    stub._voice_mode = True
    stub._voice_continuous = True
    stub._voice_recording = False
    wav = tmp_path / "barge.wav"
    wav.write_bytes(b"RIFF")
    return stub, wav


def test_barge_utterance_submits_transcript_and_cleans_wav(tmp_path):
    stub, wav = _barge_stub(tmp_path)
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "tools.voice_mode.transcribe_recording",
        return_value={"success": True, "transcript": "  hello world  "},
    ), patch("tools.voice_mode.is_voice_stop_phrase", return_value=False):
        stub._voice_submit_barge_utterance(str(wav))
    assert stub._pending_input.put.call_count == 1
    msg = stub._pending_input.put.call_args[0][0]
    assert isinstance(msg, _VoiceInputMessage)
    assert msg.text == "hello world"
    assert not wav.exists()  # cleaned up after successful transcription
    stub._voice_restart_recording_async.assert_not_called()


def test_barge_utterance_stop_phrase_disables_voice(tmp_path):
    stub, wav = _barge_stub(tmp_path)
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "tools.voice_mode.transcribe_recording",
        return_value={"success": True, "transcript": "stop"},
    ), patch("tools.voice_mode.is_voice_stop_phrase", return_value=True):
        stub._voice_submit_barge_utterance(str(wav))
    stub._disable_voice_mode.assert_called_once()
    assert stub._pending_input.put.call_count == 0


def test_barge_utterance_failure_restarts_recording(tmp_path):
    stub, wav = _barge_stub(tmp_path)
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "tools.voice_mode.transcribe_recording",
        return_value={"success": False, "error": "no speech"},
    ):
        stub._voice_submit_barge_utterance(str(wav))
    stub._voice_restart_recording_async.assert_called_once()
    assert stub._pending_input.put.call_count == 0


def test_barge_utterance_no_restart_when_voice_turned_off(tmp_path):
    stub, wav = _barge_stub(tmp_path)
    stub._voice_mode = False
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "tools.voice_mode.transcribe_recording",
        return_value={"success": False, "error": "no speech"},
    ):
        stub._voice_submit_barge_utterance(str(wav))
    stub._voice_restart_recording_async.assert_not_called()
