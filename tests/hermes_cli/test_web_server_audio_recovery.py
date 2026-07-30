"""Focused endpoint tests for durable Desktop STT recovery."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import threading

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.stt_recovery import SttRecoveryCache


@pytest.fixture
def client(_isolate_hermes_home, monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(
        hermes_state,
        "DEFAULT_DB_PATH",
        get_hermes_home() / "state.db",
    )
    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    try:
        yield test_client
    finally:
        test_client.close()


def _post_audio(client, audio: bytes = b"long irreplaceable recording"):
    encoded = base64.b64encode(audio).decode("ascii")
    return client.post(
        "/api/audio/transcribe",
        json={
            "data_url": f"data:audio/webm;base64,{encoded}",
            "mime_type": "audio/webm;codecs=opus",
        },
    )


def test_audio_transcription_success_discards_staged_recording(client, monkeypatch):
    import tools.voice_mode as voice_mode

    observed = {}

    def transcribe(path):
        observed["path"] = Path(path)
        observed["bytes"] = Path(path).read_bytes()
        return {
            "success": True,
            "transcript": "hello from voice mode",
            "provider": "test",
        }

    monkeypatch.setattr(voice_mode, "transcribe_recording", transcribe)

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "transcript": "hello from voice mode",
        "provider": "test",
    }
    assert observed["bytes"] == b"long irreplaceable recording"
    assert not observed["path"].exists()
    assert SttRecoveryCache.from_config(DEFAULT_CONFIG).list_records() == []


def test_legacy_empty_transcript_failure_is_silence_and_is_cleaned_up(
    client,
    monkeypatch,
):
    """Legacy providers omit ``no_speech`` and identify silence by error text."""
    import tools.voice_mode as voice_mode

    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": False,
            "transcript": "",
            "error": "ElevenLabs STT returned empty transcript",
            "provider": "elevenlabs",
        },
    )

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "transcript": "",
        "provider": "elevenlabs",
    }
    assert SttRecoveryCache.from_config(DEFAULT_CONFIG).list_records() == []


def test_explicit_no_speech_is_silence_and_is_cleaned_up(client, monkeypatch):
    import tools.transcription_tools as transcription_tools

    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": False,
            "transcript": "",
            "error": "provider returned no speech",
            "no_speech": True,
        },
    )

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["transcript"] == ""
    assert SttRecoveryCache.from_config(DEFAULT_CONFIG).list_records() == []


def test_provider_failure_retains_original_with_opaque_id(client, monkeypatch):
    import tools.voice_mode as voice_mode
    from hermes_constants import get_hermes_home

    secret_error = "Authorization: Bearer sk-proj-this-must-never-leak"
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": False,
            "error": secret_error,
            "provider": "openai",
        },
    )
    original = b"long irreplaceable recording"

    response = _post_audio(client, original)

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "provider_error"
    assert body["recovery_available"] is True
    assert secret_error not in response.text
    assert str(get_hermes_home()) not in response.text
    recovery_id = body["recovery_id"]
    assert len(recovery_id) == 32
    recovery_dir = get_hermes_home() / ".cache" / "stt-recovery" / recovery_id
    assert (recovery_dir / "audio.webm").read_bytes() == original
    manifest = json.loads((recovery_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["failure_code"] == "provider_error"
    assert secret_error not in json.dumps(manifest)


def test_exception_is_redacted_and_recoverable(client, monkeypatch):
    import tools.voice_mode as voice_mode
    from hermes_constants import get_hermes_home

    def fail_transcription(path):
        raise RuntimeError("api_key=sk-proj-private-value")

    monkeypatch.setattr(voice_mode, "transcribe_recording", fail_transcription)

    response = _post_audio(client)

    assert response.status_code == 500
    assert response.json()["error_code"] == "unexpected_error"
    assert response.json()["recovery_available"] is True
    assert "sk-proj-private-value" not in response.text
    assert str(get_hermes_home()) not in response.text


def test_cleanup_commit_failure_retains_original(client, monkeypatch):
    import hermes_cli.stt_recovery as stt_recovery
    import tools.voice_mode as voice_mode

    real_atomic_json_write = stt_recovery.atomic_json_write

    def fail_cleanup_commit(path, payload, *args, **kwargs):
        if payload.get("status") == "cleanup_pending":
            raise OSError("disk full")
        return real_atomic_json_write(path, payload, *args, **kwargs)

    monkeypatch.setattr(stt_recovery, "atomic_json_write", fail_cleanup_commit)
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": True,
            "transcript": "not delivered yet",
            "provider": "test",
        },
    )
    original = b"must survive cleanup commit failure"

    response = _post_audio(client, original)

    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == "cleanup_error"
    assert body["recovery_available"] is True
    assert "not delivered yet" not in response.text
    retained = SttRecoveryCache.from_config(DEFAULT_CONFIG).get_record(
        body["recovery_id"]
    )
    assert retained is not None
    assert retained.status == "failed"
    assert retained.failure_code == "cleanup_error"
    assert retained.audio_path.read_bytes() == original


def test_deferred_physical_cleanup_does_not_mask_success(client, monkeypatch):
    import hermes_cli.stt_recovery as stt_recovery
    import tools.voice_mode as voice_mode
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(
        stt_recovery.SttRecoveryCache,
        "_remove_directory",
        staticmethod(lambda path: False),
    )
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": True,
            "transcript": "cleanup may wait",
            "provider": "test",
        },
    )

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json()["transcript"] == "cleanup may wait"
    manifests = list(
        (get_hermes_home() / ".cache" / "stt-recovery").glob("*/manifest.json")
    )
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "cleanup_pending"
    assert SttRecoveryCache.from_config(DEFAULT_CONFIG).list_records() == []


def test_staging_failure_does_not_break_successful_transcription(client, monkeypatch):
    import hermes_cli.stt_recovery as stt_recovery
    import tools.voice_mode as voice_mode

    monkeypatch.setattr(
        stt_recovery.SttRecoveryCache,
        "stage_audio",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": True,
            "transcript": "fallback worked",
            "provider": "test",
        },
    )

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json()["transcript"] == "fallback worked"


def test_staging_failure_does_not_claim_recovery(client, monkeypatch):
    import hermes_cli.stt_recovery as stt_recovery
    import tools.voice_mode as voice_mode

    monkeypatch.setattr(
        stt_recovery.SttRecoveryCache,
        "stage_audio",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: {
            "success": False,
            "error": "provider unavailable",
            "provider": "test",
        },
    )

    response = _post_audio(client)

    assert response.status_code == 400
    assert response.json()["recovery_available"] is False
    assert "recovery_id" not in response.json()


@pytest.mark.parametrize(
    "result, expected_status",
    [
        (None, 500),
        (
            {
                "success": True,
                "transcript": {"not": "text"},
                "provider": "test",
            },
            500,
        ),
        (
            {
                "success": "true",
                "transcript": "must not be trusted",
                "provider": "test",
            },
            400,
        ),
    ],
)
def test_malformed_provider_results_are_recoverable(
    client,
    monkeypatch,
    result,
    expected_status,
):
    import tools.voice_mode as voice_mode

    monkeypatch.setattr(voice_mode, "transcribe_recording", lambda path: result)

    response = _post_audio(client)

    assert response.status_code == expected_status
    assert response.json()["recovery_available"] is True
    assert "must not be trusted" not in response.text


def test_request_cancellation_keeps_worker_input_retryable(monkeypatch):
    import hermes_cli.web_server as web_server
    import tools.voice_mode as voice_mode

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed = {}

    def blocking_transcription(path):
        observed["path"] = Path(path)
        started.set()
        assert release.wait(timeout=5)
        observed["bytes"] = Path(path).read_bytes()
        finished.set()
        return {"success": True, "transcript": "too late", "provider": "test"}

    monkeypatch.setattr(voice_mode, "transcribe_recording", blocking_transcription)

    async def run_and_cancel():
        payload = web_server.AudioTranscriptionRequest(
            data_url="data:audio/webm;base64,aGVsbG8=",
            mime_type="audio/webm",
        )
        task = asyncio.create_task(web_server.transcribe_audio_upload(payload))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert observed["path"].exists()
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            for _ in range(100):
                records = SttRecoveryCache.from_config(DEFAULT_CONFIG).list_records()
                if records and records[0].failure_code == "request_cancelled":
                    return records
                await asyncio.sleep(0.01)
        raise AssertionError("cancelled recording did not become retryable")

    records = asyncio.run(run_and_cancel())

    assert len(records) == 1
    assert records[0].status == "failed"
    assert records[0].failure_code == "request_cancelled"
    assert observed["bytes"] == b"hello"
