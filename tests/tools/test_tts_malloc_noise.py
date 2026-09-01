"""#54833: TTS failure messages must not be built from the benign malloc line."""

import subprocess
from types import SimpleNamespace

import pytest

_MALLOC_NOISE = (
    "python(16414) MallocStackLogging: can't turn off malloc stack logging "
    "because it was not enabled."
)


@pytest.fixture
def _darwin(monkeypatch):
    from hermes_cli import subprocess_noise

    monkeypatch.setattr(subprocess_noise, "sys", SimpleNamespace(platform="darwin"))


def _completed(stderr: str):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_neutts_failure_error_excludes_noise(_darwin, monkeypatch):
    from tools import tts_tool

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _completed(f"{_MALLOC_NOISE}\nCUDA OOM\n"),
    )
    with pytest.raises(RuntimeError) as exc:
        tts_tool._generate_neutts("hi", "/tmp/out.wav", {})
    assert "CUDA OOM" in str(exc.value)
    assert "MallocStackLogging" not in str(exc.value)


def test_neutts_noise_only_failure_keeps_generic_message(_darwin, monkeypatch):
    from tools import tts_tool

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(_MALLOC_NOISE)
    )
    with pytest.raises(RuntimeError) as exc:
        tts_tool._generate_neutts("hi", "/tmp/out.wav", {})
    assert "unknown error" in str(exc.value)


def test_piper_download_failure_excludes_noise(_darwin, monkeypatch, tmp_path):
    from tools import tts_tool

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _completed(f"{_MALLOC_NOISE}\nHTTP 404 voice missing\n"),
    )
    with pytest.raises(RuntimeError) as exc:
        tts_tool._resolve_piper_voice_path("some-voice", tmp_path)
    assert "HTTP 404" in str(exc.value)
    assert "MallocStackLogging" not in str(exc.value)
