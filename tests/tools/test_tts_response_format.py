"""
Tests for the OpenAI TTS response_format rejection + fallback + container repair.

Covers:
- ``_is_response_format_rejection``: identifies 400/422 that name
  response_format.
- ``_create_speech_with_format_fallback``: retries as mp3 on format
  rejection; propagates other errors unchanged.
- ``_repair_mp3_in_ogg_container``: detects MP3 header in .ogg path and
  transcodes via ffmpeg.
- Integration: ``_generate_openai_tts`` with a backend that rejects opus
  → falls back to mp3 → repairs the .ogg container.
"""

import base64
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_error(status_code: int, body: str) -> Exception:
    """Build an exception that looks like an OpenAI API error."""
    exc = Exception(body)
    exc.status_code = status_code  # type: ignore[attr-defined]
    return exc


# ---------------------------------------------------------------------------
# _is_response_format_rejection
# ---------------------------------------------------------------------------

class TestIsResponseFormatRejection:
    def test_422_with_response_format_in_body(self):
        from tools.tts_tool import _is_response_format_rejection
        exc = _make_error(422, '{"detail": [{"loc": ["body", "response_format"], "msg": "..."}]}')
        assert _is_response_format_rejection(exc) is True

    def test_400_with_response_format_in_body(self):
        from tools.tts_tool import _is_response_format_rejection
        exc = _make_error(400, "response_format is not supported")
        assert _is_response_format_rejection(exc) is True

    def test_422_without_response_format(self):
        from tools.tts_tool import _is_response_format_rejection
        exc = _make_error(422, "some other error")
        assert _is_response_format_rejection(exc) is False

    def test_500_ignored(self):
        from tools.tts_tool import _is_response_format_rejection
        exc = _make_error(500, "internal error")
        assert _is_response_format_rejection(exc) is False

    def test_no_status_code(self):
        from tools.tts_tool import _is_response_format_rejection
        exc = ValueError("random error")
        assert _is_response_format_rejection(exc) is False


# ---------------------------------------------------------------------------
# _create_speech_with_format_fallback
# ---------------------------------------------------------------------------

class TestCreateSpeechWithFormatFallback:
    def test_success_returns_response(self):
        from tools.tts_tool import _create_speech_with_format_fallback
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_client.audio.speech.create.return_value = mock_response

        result = _create_speech_with_format_fallback(
            mock_client, {"response_format": "opus", "model": "tts-1"}, "out.ogg"
        )
        assert result is mock_response
        mock_client.audio.speech.create.assert_called_once_with(
            response_format="opus", model="tts-1"
        )

    def test_retries_as_mp3_on_format_rejection(self):
        from tools.tts_tool import _create_speech_with_format_fallback
        mock_client = MagicMock()
        reject = _make_error(422, "response_format must be one of mp3, wav, flac, pcm")
        mock_response = MagicMock()
        mock_client.audio.speech.create.side_effect = [reject, mock_response]

        result = _create_speech_with_format_fallback(
            mock_client, {
                "response_format": "opus",
                "model": "tts-1",
                "extra_headers": {"x-idempotency-key": "first-key"},
            },
            "out.ogg"
        )
        assert result is mock_response
        assert mock_client.audio.speech.create.call_count == 2
        # First call: original format
        first_call = mock_client.audio.speech.create.call_args_list[0]
        assert first_call[1]["response_format"] == "opus"
        assert first_call[1]["extra_headers"] == {"x-idempotency-key": "first-key"}
        # Second call: retry as mp3 with fresh idempotency key
        second_call = mock_client.audio.speech.create.call_args_list[1]
        assert second_call[1]["response_format"] == "mp3"
        assert second_call[1]["extra_headers"]["x-idempotency-key"] != "first-key"

    def test_passes_through_on_mp3_request(self):
        """When the original request is already mp3, don't attempt a retry."""
        from tools.tts_tool import _create_speech_with_format_fallback
        mock_client = MagicMock()
        reject = _make_error(422, "response_format error")
        mock_client.audio.speech.create.side_effect = reject

        with pytest.raises(Exception):
            _create_speech_with_format_fallback(
                mock_client, {"response_format": "mp3", "model": "tts-1"}, "out.mp3"
            )
        assert mock_client.audio.speech.create.call_count == 1

    def test_passes_through_on_non_format_rejection(self):
        """Non-format errors (e.g. auth, rate-limit) propagate."""
        from tools.tts_tool import _create_speech_with_format_fallback
        mock_client = MagicMock()
        reject = _make_error(401, "Invalid API key")
        mock_client.audio.speech.create.side_effect = reject

        with pytest.raises(Exception):
            _create_speech_with_format_fallback(
                mock_client, {"response_format": "opus", "model": "tts-1"}, "out.ogg"
            )
        assert mock_client.audio.speech.create.call_count == 1


# ---------------------------------------------------------------------------
# _repair_mp3_in_ogg_container
# ---------------------------------------------------------------------------

class TestRepairMp3InOggContainer:
    def test_skips_when_not_mp3(self, tmp_path):
        """A real Ogg file header should not trigger repair."""
        from tools.tts_tool import _repair_mp3_in_ogg_container
        ogg_path = tmp_path / "voice.ogg"
        # Write a fake Ogg page header (OggS magic)
        ogg_path.write_bytes(b"OggS" + b"\x00" * 100)
        _repair_mp3_in_ogg_container(str(ogg_path))
        # File unchanged
        assert ogg_path.read_bytes()[:4] == b"OggS"

    def test_skips_when_file_missing_or_empty(self, tmp_path):
        from tools.tts_tool import _repair_mp3_in_ogg_container
        missing = str(tmp_path / "nonexistent.ogg")
        # Should not raise
        _repair_mp3_in_ogg_container(missing)

        empty = tmp_path / "empty.ogg"
        empty.touch()
        _repair_mp3_in_ogg_container(str(empty))
        assert empty.stat().st_size == 0

    def test_detects_id3_header(self, tmp_path):
        """MP3 with an ID3v2 tag should be detected as needing repair."""
        from tools.tts_tool import _repair_mp3_in_ogg_container
        ogg_path = tmp_path / "voice.ogg"
        ogg_path.write_bytes(b"ID3" + b"\x00" * 200)
        # Without ffmpeg, it should log a warning but not crash
        with patch("tools.tts_tool._has_ffmpeg", return_value=False):
            _repair_mp3_in_ogg_container(str(ogg_path))
        # File still exists (warning path)
        assert ogg_path.exists()

    def test_transcodes_with_ffmpeg(self, tmp_path):
        """With ffmpeg available, the file is transcoded to proper Opus."""
        from tools.tts_tool import _repair_mp3_in_ogg_container
        ogg_path = tmp_path / "voice.ogg"
        ogg_path.write_bytes(b"ID3" + b"fake mp3 data here")

        def fake_convert(mp3_path):
            # _convert_to_opus strips .mp3 and renames to .ogg
            result = str(mp3_path).rsplit(".", 1)[0] + ".ogg"
            with open(result, "wb") as f:
                f.write(b"OggS real opus data")
            return result

        with patch("tools.tts_tool._has_ffmpeg", return_value=True),              patch("tools.tts_tool._convert_to_opus", side_effect=fake_convert):
            _repair_mp3_in_ogg_container(str(ogg_path))

        # The file should now contain proper Ogg/Opus data
        assert ogg_path.exists()
        assert ogg_path.read_bytes() == b"OggS real opus data"


# ---------------------------------------------------------------------------
# Integration: _generate_openai_tts with format rejection
# ---------------------------------------------------------------------------

class TestGenerateOpenaiTtsResponseFormatFallback:
    """End-to-end test of the response_format fallback chain."""

    def test_fallback_to_mp3_on_opus_rejection(self, tmp_path, monkeypatch):
        """When backend rejects opus, retries as mp3 and repairs the .ogg container."""
        reject = _make_error(422, "response_format must be 'mp3', 'flac', 'wav' or 'pcm'")

        call_count = [0]
        mock_response = MagicMock()

        def speech_create_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1 and kwargs.get("response_format") == "opus":
                raise reject
            return mock_response

        mock_client = MagicMock()
        mock_client.audio.speech.create = speech_create_side_effect

        mock_cls = MagicMock(return_value=mock_client)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr("tools.tts_tool.uuid", uuid)

        with patch("tools.tts_tool._import_openai_client", return_value=mock_cls),              patch("tools.tts_tool._resolve_openai_audio_client_config",
                   return_value=("test-key", None, False)):

            from tools.tts_tool import _generate_openai_tts
            output_path = str(tmp_path / "voice.ogg")
            _generate_openai_tts("Hello", output_path, {"openai": {}})

        # Second call was with mp3
        assert call_count[0] == 2

    def test_opus_succeeds_normally_no_fallback(self, tmp_path, monkeypatch):
        """When backend supports opus, no fallback or repair needed."""
        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.audio.speech.create.return_value = mock_response
        mock_cls = MagicMock(return_value=mock_client)

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        with patch("tools.tts_tool._import_openai_client", return_value=mock_cls),              patch("tools.tts_tool._resolve_openai_audio_client_config",
                   return_value=("test-key", None, False)):

            from tools.tts_tool import _generate_openai_tts
            output_path = str(tmp_path / "voice.ogg")
            result = _generate_openai_tts("Hello", output_path, {"openai": {}})

        assert result == output_path
        mock_client.audio.speech.create.assert_called_once()
        assert mock_client.audio.speech.create.call_args[1]["response_format"] == "opus"

    def test_mp3_path_no_fallback_attempted(self, tmp_path, monkeypatch):
        """When output is .mp3, the initial format is already mp3 — no retry needed."""
        reject = _make_error(422, "response_format error")
        mock_client = MagicMock()
        mock_client.audio.speech.create.side_effect = reject
        mock_cls = MagicMock(return_value=mock_client)

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        with patch("tools.tts_tool._import_openai_client", return_value=mock_cls),              patch("tools.tts_tool._resolve_openai_audio_client_config",
                   return_value=("test-key", None, False)):

            from tools.tts_tool import _generate_openai_tts
            output_path = str(tmp_path / "voice.mp3")
            with pytest.raises(Exception):
                _generate_openai_tts("Hello", output_path, {"openai": {}})

        # Only one call attempt — no retry because format was already mp3
        assert mock_client.audio.speech.create.call_count == 1
