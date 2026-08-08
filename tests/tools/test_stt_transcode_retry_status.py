"""Container rejections must reach the transcode retry whatever status they carry.

The transcode-and-retry fallback (#68732) was gated on ``BadRequestError``, so
it only ever ran for HTTP 400. OpenAI-compatible providers that answer a
container they cannot read with a 5xx never reached it: the desktop's ``.webm``
dictation blobs failed permanently even with ffmpeg installed and a one-line
transcode available (#81644).

The 5xx bodies carry no usable text (a bare ``system_error``), so widening the
keyword gate cannot help; the status itself has to be part of the decision.
"""

from __future__ import annotations

import pytest

class _Err(Exception):
    """Stands in for openai.APIStatusError, which exposes ``status_code``."""

    def __init__(self, status_code, message=""):
        super().__init__(message)
        self.status_code = status_code


def test_generic_5xx_container_rejection_now_retries():
    """The reported shape: 503 with a body that names nothing."""
    from tools.transcription_tools import _should_transcode_and_retry

    exc = _Err(503, '{"error":{"message":"request failed","type":"system_error"}}')

    assert _should_transcode_and_retry(exc) is True


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_every_server_status_reaches_the_retry(status):
    from tools.transcription_tools import _should_transcode_and_retry

    assert _should_transcode_and_retry(_Err(status, "boom")) is True


def test_400_still_needs_a_container_hint():
    """Guard: the historical keyword gate for 400 is unchanged."""
    from tools.transcription_tools import _should_transcode_and_retry

    assert _should_transcode_and_retry(_Err(400, "Unsupported file format")) is True
    assert _should_transcode_and_retry(_Err(400, "audio file is corrupted")) is True
    assert _should_transcode_and_retry(_Err(400, "Invalid file provided")) is True


def test_400_without_a_container_hint_still_raises():
    """Guard: a quota or validation 400 must not spend a transcode."""
    from tools.transcription_tools import _should_transcode_and_retry

    assert _should_transcode_and_retry(_Err(400, "You exceeded your quota")) is False


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429])
def test_other_client_errors_are_untouched(status):
    """Guard: widening to APIStatusError must not swallow auth or rate limits."""
    from tools.transcription_tools import _should_transcode_and_retry

    assert _should_transcode_and_retry(_Err(status, "unsupported")) is False


def test_missing_status_does_not_retry():
    """An exception without a status tells us nothing; re-raise rather than guess."""
    from tools.transcription_tools import _should_transcode_and_retry


    class _NoStatus(Exception):
        pass

    assert _should_transcode_and_retry(_NoStatus("unsupported")) is False
    assert _should_transcode_and_retry(_Err(None, "unsupported")) is False


def _api_status_error(status: int, message: str):
    """A real openai.APIStatusError, so the test binds to the SDK's shape."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.invalid/v1/audio/transcriptions")
    response = httpx.Response(status, request=request, text=message)
    return openai.APIStatusError(message, response=response, body=None)


def test_end_to_end_5xx_reaches_the_transcode_retry(tmp_path, monkeypatch):
    """Behavioural probe: the real handler must attempt the transcode on 503.

    Drives _transcribe_openai with a client that rejects the original container
    with 503 and accepts the transcoded one, which is the reported scenario.
    """
    import openai

    from tools import transcription_tools as tt

    src = tmp_path / "dictation.webm"
    src.write_bytes(b"fake-webm")
    converted = tmp_path / "converted.m4a"
    converted.write_bytes(b"fake-m4a")

    calls: list[str] = []
    transcoded: list[str] = []

    class _Transcriptions:
        def create(self, **kwargs):
            name = getattr(kwargs["file"], "name", "")
            calls.append(name)
            if name.endswith(".webm"):
                raise _api_status_error(
                    503, '{"error":{"message":"request failed","type":"system_error"}}'
                )
            return type("_T", (), {"text": "salam donya"})()

    class _Client:
        audio = type("_A", (), {"transcriptions": _Transcriptions()})()

        def close(self):
            pass

    monkeypatch.setattr(openai, "OpenAI", lambda **_kw: _Client())

    def _fake_transcode(file_path, work_dir):
        transcoded.append(file_path)
        return str(converted), None

    monkeypatch.setattr(tt, "_transcode_audio_for_stt", _fake_transcode)

    result = tt._transcribe_openai(str(src), "whisper-1", api_key="k")

    assert transcoded, (
        "a 503 container rejection never reached the transcode fallback; "
        f"upload attempts were {calls}"
    )
    assert result.get("success") is True
    assert result.get("transcript") == "salam donya"
