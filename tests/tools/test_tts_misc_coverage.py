"""Catch-all coverage for misc/unassigned helpers in ``tools.tts_tool``.

This is the misc/ffmpeg cluster: the ffmpeg container helpers
(``_convert_to_opus``, ``_ffmpeg_transcode_to_opus``, ``_repair_ogg_container``
fallback, ``_concat_audio_files``) plus the small response/section utility
helpers (``_config_bool``, ``_response_has_explicit_stream``, ``_close_response``,
``_read_tts_response_bytes``, ``_read_tts_response_json``,
``_write_tts_response_to_file``).  Everything is headerless and mocked so no
network, no API keys and no multi-GB download is exercised.
"""

import os
import shlex
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools import tts_tool


# ---------------------------------------------------------------------------
# ffmpeg presence / Opus conversion
# ---------------------------------------------------------------------------

class TestConvertToOpus:
    def test_returns_none_without_ffmpeg(self):
        with patch.object(tts_tool, "_has_ffmpeg", return_value=False):
            assert tts_tool._convert_to_opus("/tmp/a.mp3") is None

    def test_derives_ogg_path_and_transcodes(self, tmp_path):
        inp = tmp_path / "a.mp3"
        out = tmp_path / "a.ogg"
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool, "_ffmpeg_transcode_to_opus", return_value=str(out)) as tr:
            assert tts_tool._convert_to_opus(str(inp)) == str(out)
        tr.assert_called_once_with(str(inp), str(out))


class TestFfmpegTranscodeToOpus:
    def _make_input(self, tmp_path):
        p = tmp_path / "in.mp3"
        p.write_bytes(b"mp3payload")
        return p

    def test_without_ffmpeg_returns_none(self):
        with patch.object(tts_tool, "_has_ffmpeg", return_value=False):
            assert tts_tool._ffmpeg_transcode_to_opus("a.mp3", "b.ogg") is None

    def test_success_writes_ogg(self, tmp_path):
        inp = self._make_input(tmp_path)
        out = tmp_path / "out.ogg"
        result = MagicMock(returncode=0, stderr=b"")
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", return_value=result) as run:
            out.write_bytes(b"some-opus")  # stand in for ffmpeg output
            assert tts_tool._ffmpeg_transcode_to_opus(str(inp), str(out)) == str(out)
        cmd = run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "-i" in cmd and str(inp) in cmd
        assert "libopus" in cmd
        assert "-ac" in cmd and "1" in cmd

    def test_in_place_transcode(self, tmp_path):
        p = tmp_path / "in.ogg"
        p.write_bytes(b"mp3payload")
        work = tmp_path / "in.ogg.tmp.ogg"

        def fake_run(cmd, **kwargs):
            work.write_bytes(b"real-opus")
            return MagicMock(returncode=0, stderr=b"")

        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", side_effect=fake_run):
            assert tts_tool._ffmpeg_transcode_to_opus(str(p), str(p)) == str(p)
        assert not work.exists()
        assert p.read_bytes() == b"real-opus"

    def test_ffmpeg_nonzero_returns_none(self, tmp_path):
        inp = self._make_input(tmp_path)
        result = MagicMock(returncode=3, stderr=b"error")
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", return_value=result):
            assert tts_tool._ffmpeg_transcode_to_opus(str(inp), str(tmp_path / "o.ogg")) is None

    def test_timeout_returns_none(self, tmp_path):
        inp = self._make_input(tmp_path)
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            assert tts_tool._ffmpeg_transcode_to_opus(str(inp), str(tmp_path / "o.ogg")) is None

    def test_file_not_found_returns_none(self, tmp_path):
        inp = self._make_input(tmp_path)
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", side_effect=FileNotFoundError()):
            assert tts_tool._ffmpeg_transcode_to_opus(str(inp), str(tmp_path / "o.ogg")) is None

    def test_generic_exception_returns_none(self, tmp_path):
        inp = self._make_input(tmp_path)
        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", side_effect=RuntimeError("boom")):
            assert tts_tool._ffmpeg_transcode_to_opus(str(inp), str(tmp_path / "o.ogg")) is None

    def test_in_place_cleans_temp_on_error(self, tmp_path):
        p = tmp_path / "in.ogg"
        p.write_bytes(b"mp3payload")
        work = tmp_path / "in.ogg.tmp.ogg"

        def fake_run(cmd, **kwargs):
            work.write_bytes(b"partial")
            raise subprocess.TimeoutExpired("ffmpeg", 30)

        with patch.object(tts_tool, "_has_ffmpeg", return_value=True), \
             patch.object(tts_tool.subprocess, "run", side_effect=fake_run):
            assert tts_tool._ffmpeg_transcode_to_opus(str(p), str(p)) is None
        assert not work.exists()


# ---------------------------------------------------------------------------
# Container repair fallback (ffmpeg unavailable + honest rename)
# ---------------------------------------------------------------------------

class TestRepairOggContainerFallback:
    def test_ffmpeg_failure_renames_to_honest_extension(self, tmp_path):
        p = tmp_path / "v.ogg"
        p.write_bytes(b"ID3xxxx")
        with patch.object(tts_tool, "_sniff_audio_container", return_value="mp3"), \
             patch.object(tts_tool, "_ffmpeg_transcode_to_opus", return_value=None):
            assert tts_tool._repair_ogg_container(str(p)) == str(tmp_path / "v.mp3")
        assert not p.exists()
        assert (tmp_path / "v.mp3").exists()

    def test_rename_oserror_keeps_original_path(self, tmp_path):
        p = tmp_path / "v.ogg"
        p.write_bytes(b"ID3xxxx")
        with patch.object(tts_tool, "_sniff_audio_container", return_value="flac"), \
             patch.object(tts_tool, "_ffmpeg_transcode_to_opus", return_value=None), \
             patch.object(tts_tool.os, "replace", side_effect=OSError("no")):
            assert tts_tool._repair_ogg_container(str(p)) == str(p)
        assert p.exists()


# ---------------------------------------------------------------------------
# ffmpeg multi-chunk concatenation
# ---------------------------------------------------------------------------

class TestConcatAudioFiles:
    def _inputs(self, tmp_path, suffix=".mp3"):
        a = tmp_path / ("a" + suffix)
        b = tmp_path / ("b" + suffix)
        a.write_bytes(b"chunk1")
        b.write_bytes(b"chunk2")
        return str(a), str(b)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            tts_tool._concat_audio_files([], "out.mp3")

    def test_single_file_copies_to_output(self, tmp_path):
        src = tmp_path / "a.mp3"
        src.write_bytes(b"audio")
        out = tmp_path / "b.mp3"
        with patch.object(tts_tool.shutil, "copyfile", wraps=shutil.copyfile) as cf:
            assert tts_tool._concat_audio_files([str(src)], str(out)) == str(out)
        cf.assert_called_once_with(str(src), str(out))
        assert out.read_bytes() == b"audio"

    def test_single_file_same_path_skips_copy(self, tmp_path):
        src = tmp_path / "a.mp3"
        src.write_bytes(b"audio")
        with patch.object(tts_tool.shutil, "copyfile") as cf:
            assert tts_tool._concat_audio_files([str(src)], str(src)) == str(src)
        cf.assert_not_called()

    def test_no_ffmpeg_returns_none(self, tmp_path):
        a, b = self._inputs(tmp_path)
        with patch.object(tts_tool.shutil, "which", return_value=None):
            assert tts_tool._concat_audio_files([a, b], str(tmp_path / "out.mp3")) is None

    def _run_concat(self, inputs, output, *, voice_compatible=False):
        """Run _concat_audio_files with a fake ffmpeg that writes cmd[-1]."""
        result = MagicMock(returncode=0, stderr=b"")

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"combined")
            return result

        with patch.object(tts_tool.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(tts_tool.subprocess, "run", side_effect=fake_run) as run:
            out = tts_tool._concat_audio_files(
                list(inputs), output, voice_compatible=voice_compatible,
            )
        return out, run.call_args[0][0]

    def test_voice_compatible_uses_libopus(self, tmp_path):
        out = str(tmp_path / "out.ogg")
        a, b = self._inputs(tmp_path)
        returned, cmd = self._run_concat([a, b], out, voice_compatible=True)
        assert returned == out
        assert "libopus" in cmd
        assert "-ac" in cmd and "1" in cmd
        assert "-b:a" in cmd and "64k" in cmd
        assert Path(out).read_bytes() == b"combined"

    def test_ogg_suffix_uses_libopus(self, tmp_path):
        # A .ogg destination opts in even without the voice flag.
        a, b = self._inputs(tmp_path)
        out = str(tmp_path / "out.ogg")
        returned, cmd = self._run_concat([a, b], out, voice_compatible=False)
        assert returned == out
        assert "libopus" in cmd

    def test_matching_mp3_chunks_preserve_frames(self, tmp_path):
        a, b = self._inputs(tmp_path)
        out = str(tmp_path / "out.mp3")
        returned, cmd = self._run_concat([a, b], out, voice_compatible=False)
        assert returned == out
        assert "-c:a" in cmd and "copy" in cmd

    def test_other_container_uses_default_codec(self, tmp_path):
        # .wav destination with .mp3 sources: neither voice nor matching mp3.
        a, b = self._inputs(tmp_path)
        out = str(tmp_path / "out.wav")
        returned, cmd = self._run_concat([a, b], out, voice_compatible=False)
        assert returned == out
        assert "-c:a" not in cmd

    def test_concat_writes_shlex_quoted_absolute_paths(self, tmp_path):
        a, b = self._inputs(tmp_path)
        captured = {}

        def fake_run(cmd, **kwargs):
            # cmd[9] is the concat list file.
            captured["concat"] = Path(cmd[9]).read_text()
            Path(cmd[-1]).write_bytes(b"combined")
            return MagicMock(returncode=0, stderr=b"")

        with patch.object(tts_tool.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(tts_tool.subprocess, "run", side_effect=fake_run):
            tts_tool._concat_audio_files(
                [a, b], str(tmp_path / "out.mp3"), voice_compatible=False,
            )
        expected = "".join(f"file {shlex.quote(os.path.abspath(p))}\n" for p in (a, b))
        assert captured["concat"] == expected

    def test_ffmpeg_failure_returns_none(self, tmp_path):
        a, b = self._inputs(tmp_path)
        result = MagicMock(returncode=1, stderr=b"err")
        with patch.object(tts_tool.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(tts_tool.subprocess, "run", return_value=result):
            assert tts_tool._concat_audio_files([a, b], str(tmp_path / "out.mp3")) is None

    def test_exception_returns_none(self, tmp_path):
        a, b = self._inputs(tmp_path)
        with patch.object(tts_tool.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             patch.object(tts_tool.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ffmpeg", 120)):
            assert tts_tool._concat_audio_files([a, b], str(tmp_path / "out.mp3")) is None


# ---------------------------------------------------------------------------
# Small misc utility helpers
# ---------------------------------------------------------------------------

class TestConfigBool:
    def test_bool_identity(self):
        assert tts_tool._config_bool(True) is True
        assert tts_tool._config_bool(False) is False

    def test_none_uses_default(self):
        assert tts_tool._config_bool(None) is False
        assert tts_tool._config_bool(None, default=True) is True

    def test_numeric(self):
        assert tts_tool._config_bool(1) is True
        assert tts_tool._config_bool(0) is False
        assert tts_tool._config_bool(2.5) is True

    @pytest.mark.parametrize("v", ["1", "true", "YES", "  on ", "enabled"])
    def test_truthy_spellings(self, v):
        assert tts_tool._config_bool(v) is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "disabled"])
    def test_falsy_spellings(self, v):
        assert tts_tool._config_bool(v) is False

    def test_random_string_returns_default(self):
        assert tts_tool._config_bool("random") is False
        assert tts_tool._config_bool("random", default=True) is True


class _NonRequestStream:
    def iter_content(self, chunk_size=65536):
        yield from ()


class _RequestStream:
    def iter_content(self, chunk_size=65536):
        yield from ()


_RequestStream.__module__ = "requests.models"


class _NoStream:
    content = b"data"


class _NonCallableIter:
    iter_content = b"not callable"


class TestResponseHasExplicitStream:
    def test_requests_module_true(self):
        assert tts_tool._response_has_explicit_stream(_RequestStream()) is True

    def test_non_requests_with_iter_content_true(self):
        assert tts_tool._response_has_explicit_stream(_NonRequestStream()) is True

    def test_no_iter_content_false(self):
        assert tts_tool._response_has_explicit_stream(_NoStream()) is False

    def test_non_callable_iter_content_false(self):
        assert tts_tool._response_has_explicit_stream(_NonCallableIter()) is False


class TestCloseResponse:
    def test_calls_close(self):
        calls = []

        class Resp:
            def close(self):
                calls.append(True)

        tts_tool._close_response(Resp())
        assert calls == [True]

    def test_close_error_is_suppressed(self):
        class Resp:
            def close(self):
                raise RuntimeError("boom")

        tts_tool._close_response(Resp())  # should not raise

    def test_no_close_method(self):
        tts_tool._close_response(object())  # should not raise


class _StreamingResp:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        self.closed = True


class _StrChunkStream:
    def iter_content(self, chunk_size=65536):
        yield "abc"

    def close(self):
        pass


class _OverLimitStream:
    def __init__(self):
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield b"12345"
        yield b"6789"

    def close(self):
        self.closed = True


class _ContentStr:
    content = "hello"


class TestReadTtsResponseBytes:
    def test_streaming_joins_chunks(self, monkeypatch):
        monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 1000)
        resp = _StreamingResp([b"abc", b"def"])
        assert tts_tool._read_tts_response_bytes(resp, label="x") == b"abcdef"
        assert resp.closed is True

    def test_non_stream_content_str_encoded(self):
        assert tts_tool._read_tts_response_bytes(_ContentStr(), label="x") == b"hello"

    def test_str_chunk_is_encoded(self, monkeypatch):
        monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 1000)
        assert tts_tool._read_tts_response_bytes(_StrChunkStream(), label="x") == b"abc"

    def test_over_limit_raises_and_closes(self, monkeypatch):
        monkeypatch.setattr(tts_tool, "TTS_RESPONSE_BODY_LIMIT_BYTES", 8)
        resp = _OverLimitStream()
        with pytest.raises(RuntimeError, match="x response exceeds 8 bytes"):
            tts_tool._read_tts_response_bytes(resp, label="x")
        assert resp.closed is True


class _JsonByIdentity:
    content = b'{"a": 1}'


class _EmptyJson:
    content = b""

    def json(self):
        return {"b": 2}


class _EmptyNonDictJson:
    content = b""

    def json(self):
        return [1, 2, 3]


class _EmptyNoJson:
    content = b""


class TestReadTtsResponseJson:
    def test_decodes_json_body(self):
        assert tts_tool._read_tts_response_json(_JsonByIdentity(), label="x") == {"a": 1}

    def test_empty_falls_back_to_json_method(self):
        assert tts_tool._read_tts_response_json(_EmptyJson(), label="x") == {"b": 2}

    def test_non_dict_json_returns_empty(self):
        assert tts_tool._read_tts_response_json(_EmptyNonDictJson(), label="x") == {}

    def test_empty_no_json_method_returns_empty(self):
        assert tts_tool._read_tts_response_json(_EmptyNoJson(), label="x") == {}


class TestWriteTtsResponseToFile:
    def test_writes_bytes_to_output(self, tmp_path):
        out = tmp_path / "o.mp3"
        tts_tool._write_tts_response_to_file(_StreamingResp([b"abc"]), str(out), label="x")
        assert out.read_bytes() == b"abc"
