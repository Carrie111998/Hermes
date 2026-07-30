from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.stt_recovery import SttRecoveryCache
from hermes_cli import stt_recovery_cli


def _failed_record(audio: bytes = b"voice"):
    cache = SttRecoveryCache.from_config(DEFAULT_CONFIG)
    staged = cache.stage_audio(audio, suffix=".webm", mime_type="audio/webm")
    assert staged is not None
    failed = cache.mark_failed_attempt(
        staged.recovery_id,
        attempts=staged.attempts,
        failure_code="provider_error",
    )
    assert failed is not None
    return cache, failed


def test_parser_registers_documented_command_tree():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    stt_recovery_cli.register_cli(subparsers)

    args = parser.parse_args(["stt", "recovery", "retry", "a" * 32])

    assert args.command == "stt"
    assert args.stt_command == "recovery"
    assert args.recovery_command == "retry"
    assert args.func is stt_recovery_cli._cmd_retry


def test_list_json_never_exposes_storage_path(_isolate_hermes_home, capsys):
    _, failed = _failed_record()

    assert stt_recovery_cli._cmd_list(SimpleNamespace(json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["recovery_id"] == failed.recovery_id
    assert "path" not in payload[0]


def test_retry_prints_transcript_before_discard(
    _isolate_hermes_home,
    monkeypatch,
    capsys,
):
    import tools.transcription_tools as transcription_tools

    cache, failed = _failed_record()
    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": True,
            "transcript": "recovered transcript",
            "provider": "test",
        },
    )

    result = stt_recovery_cli._cmd_retry(
        SimpleNamespace(recovery_id=failed.recovery_id)
    )

    assert result == 0
    assert capsys.readouterr().out == "recovered transcript\n"
    assert cache.get_record(failed.recovery_id) is None


def test_failed_retry_redacts_error_and_keeps_original(
    _isolate_hermes_home,
    monkeypatch,
    capsys,
):
    import tools.transcription_tools as transcription_tools

    cache, failed = _failed_record(b"important")
    secret = "sk-proj-this-must-not-print"
    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": False,
            "error": f"Authorization: Bearer {secret}",
            "provider": "openai",
        },
    )

    result = stt_recovery_cli._cmd_retry(
        SimpleNamespace(recovery_id=failed.recovery_id)
    )

    assert result == 1
    stderr = capsys.readouterr().err
    assert secret not in stderr
    retained = cache.get_record(failed.recovery_id)
    assert retained is not None
    assert retained.audio_path.read_bytes() == b"important"


def test_empty_retry_keeps_original(_isolate_hermes_home, monkeypatch, capsys):
    import tools.transcription_tools as transcription_tools

    cache, failed = _failed_record(b"possibly speech")
    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": True,
            "transcript": "",
            "provider": "test",
        },
    )

    result = stt_recovery_cli._cmd_retry(
        SimpleNamespace(recovery_id=failed.recovery_id)
    )

    assert result == 1
    assert "still retained" in capsys.readouterr().err
    retained = cache.get_record(failed.recovery_id)
    assert retained is not None
    assert retained.failure_code == "no_speech"


def test_malformed_provider_result_keeps_original(
    _isolate_hermes_home,
    monkeypatch,
    capsys,
):
    import tools.voice_mode as voice_mode

    cache, failed = _failed_record(b"important")
    monkeypatch.setattr(
        voice_mode,
        "transcribe_recording",
        lambda path: None,
    )

    result = stt_recovery_cli._cmd_retry(
        SimpleNamespace(recovery_id=failed.recovery_id)
    )

    assert result == 1
    assert "invalid result" in capsys.readouterr().err
    retained = cache.get_record(failed.recovery_id)
    assert retained is not None
    assert retained.status == "failed"
    assert retained.failure_code == "unexpected_error"
    assert retained.audio_path.read_bytes() == b"important"


def test_truthy_non_boolean_success_keeps_original(
    _isolate_hermes_home,
    monkeypatch,
    capsys,
):
    import tools.transcription_tools as transcription_tools

    cache, failed = _failed_record(b"important")
    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": "true",
            "transcript": "must not be trusted",
            "provider": "test",
        },
    )

    result = stt_recovery_cli._cmd_retry(
        SimpleNamespace(recovery_id=failed.recovery_id)
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "must not be trusted" not in captured.out
    retained = cache.get_record(failed.recovery_id)
    assert retained is not None
    assert retained.failure_code == "provider_error"
    assert retained.audio_path.read_bytes() == b"important"


def test_stdout_flush_failure_leaves_retryable_record(
    _isolate_hermes_home,
    monkeypatch,
):
    import tools.transcription_tools as transcription_tools

    cache, failed = _failed_record(b"important")
    monkeypatch.setattr(
        transcription_tools,
        "transcribe_audio",
        lambda path, model=None: {
            "success": True,
            "transcript": "recovered transcript",
            "provider": "test",
        },
    )

    class BrokenStdout:
        def write(self, value):
            return len(value)

        def flush(self):
            raise BrokenPipeError("consumer closed")

    original_stdout = stt_recovery_cli.sys.stdout
    try:
        stt_recovery_cli.sys.stdout = BrokenStdout()
        with pytest.raises(BrokenPipeError):
            stt_recovery_cli._cmd_retry(SimpleNamespace(recovery_id=failed.recovery_id))
    finally:
        stt_recovery_cli.sys.stdout = original_stdout

    retained = cache.get_record(failed.recovery_id)
    assert retained is not None
    assert retained.status == "failed"
    assert retained.failure_code == "delivery_interrupted"
    assert retained.audio_path.read_bytes() == b"important"


def test_save_exports_exact_bytes_privately_without_discard(
    _isolate_hermes_home,
    tmp_path,
    capsys,
):
    cache, failed = _failed_record(b"original-container")
    destination = tmp_path / "saved.webm"

    result = stt_recovery_cli._cmd_save(
        SimpleNamespace(
            recovery_id=failed.recovery_id,
            output=str(destination),
            force=False,
        )
    )

    assert result == 0
    assert destination.read_bytes() == b"original-container"
    assert cache.get_record(failed.recovery_id) is not None
    assert str(destination) in capsys.readouterr().out
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_save_refuses_overwrite_without_force(
    _isolate_hermes_home,
    tmp_path,
    capsys,
):
    _, failed = _failed_record()
    destination = tmp_path / "existing.webm"
    destination.write_bytes(b"keep")

    result = stt_recovery_cli._cmd_save(
        SimpleNamespace(
            recovery_id=failed.recovery_id,
            output=str(destination),
            force=False,
        )
    )

    assert result == 1
    assert destination.read_bytes() == b"keep"
    assert "--force" in capsys.readouterr().err


def test_discard_rejects_non_opaque_id(_isolate_hermes_home, capsys):
    result = stt_recovery_cli._cmd_discard(
        SimpleNamespace(recovery_id="../../config.yaml")
    )

    assert result == 1
    assert "not found" in capsys.readouterr().err
