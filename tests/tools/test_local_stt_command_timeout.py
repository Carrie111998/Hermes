"""Timeout-safety tests for the local-STT shell-out in ``tools.transcription_tools``.

``_transcribe_local_command`` runs a whisper CLI under a 300s budget. Whisper
shells out to ffmpeg, so the STT command spawns a *grandchild* that inherits the
capture handles — and with a user-supplied ``HERMES_LOCAL_STT_COMMAND`` template
it runs under ``shell=True``, where the grandchild is guaranteed rather than
merely likely (cmd.exe is then the direct child). Under
``subprocess.run(capture_output=True, timeout=N)`` that grandchild holds the
pipe's write end open so the drain never reaches EOF and the timeout cannot fire
on Windows. The site therefore uses the file-backed
``_subprocess_compat.run_text_capture`` instead.

The helper has no ``check=`` and never raises ``CalledProcessError``, so the site
calls ``CompletedProcess.check_returncode()`` explicitly. These tests pin that:
losing it would silently turn a failed transcription into a "no .txt produced"
error that hides the command's own stderr.
"""

import subprocess

import pytest

from tools import transcription_tools


@pytest.fixture
def _local_command(monkeypatch, tmp_path):
    """Point the local-STT path at a template and a native-format input.

    A ``.wav`` suffix keeps ``_prepare_local_audio`` from shelling out to ffmpeg,
    so these tests exercise the STT call and nothing else.
    """
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVE")
    monkeypatch.setattr(
        transcription_tools, "_get_local_command_template", lambda: "whisper {input_path}"
    )
    return str(audio)


def test_local_stt_uses_the_file_backed_capture_helper(monkeypatch, _local_command):
    seen = {}

    def _fake(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        # Nothing writes a .txt, so the call reports "no transcript" — this test
        # only pins HOW the command was run.
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transcription_tools, "run_text_capture", _fake)

    transcription_tools._transcribe_local_command(_local_command, "base")

    assert seen["kwargs"]["timeout"] == 300
    # No env var set -> auto-detected template -> list mode, not shell.
    assert seen["kwargs"]["shell"] is False
    assert isinstance(seen["command"], list)
    # The helper supplies CREATE_NO_WINDOW itself; passing it again is an error.
    assert "creationflags" not in seen["kwargs"]


def test_local_stt_shell_mode_passes_a_command_string(monkeypatch, _local_command):
    """A user-supplied template runs under ``shell=True`` and must stay a STRING.

    ``list()`` over a command string would shred it into one argument per
    character — the exact reason the helper needed a shell parameter.
    """
    monkeypatch.setenv(transcription_tools.LOCAL_STT_COMMAND_ENV, "whisper {input_path}")
    seen = {}

    def _fake(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transcription_tools, "run_text_capture", _fake)

    transcription_tools._transcribe_local_command(_local_command, "base")

    assert seen["kwargs"]["shell"] is True
    assert isinstance(seen["command"], str)


def test_local_stt_nonzero_exit_still_surfaces_the_commands_stderr(
    monkeypatch, _local_command
):
    """``check_returncode()`` replaces the old ``check=True``.

    Without it a failing STT command falls through to the .txt glob and reports
    "did not produce a .txt transcript", burying the real cause.
    """
    monkeypatch.setattr(
        transcription_tools,
        "run_text_capture",
        lambda command, **kwargs: subprocess.CompletedProcess(
            args=command, returncode=2, stdout="", stderr="model weights missing"
        ),
    )

    result = transcription_tools._transcribe_local_command(_local_command, "base")

    assert result["success"] is False
    assert "model weights missing" in result["error"]


def test_local_stt_timeout_is_reported_not_raised(monkeypatch, _local_command):
    """A timed-out STT command must come back as a failed result, not an exception."""

    def _timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 300)

    monkeypatch.setattr(transcription_tools, "run_text_capture", _timeout)

    result = transcription_tools._transcribe_local_command(_local_command, "base")

    assert result["success"] is False
    assert result["transcript"] == ""
