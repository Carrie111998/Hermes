"""Transient faster-whisper worker-mode tests.

The tests exercise the parent/worker contract with a deterministic fake child;
no faster-whisper package or model download is required.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools.transcription_tools as stt


class _FakeWorkerProcess:
    def __init__(self, *, returncode=0, response_path=None, response=None, timeout=False):
        self.pid = 4242
        self.returncode = None
        self._final_returncode = returncode
        self._response_path = response_path
        self._response = response
        self._timeout = timeout
        self.wait_calls = []
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._timeout:
            raise subprocess.TimeoutExpired("local-stt-worker", timeout)
        if self._response_path is not None and self._response is not None:
            Path(self._response_path).write_text(
                json.dumps(self._response), encoding="utf-8"
            )
        self.returncode = self._final_returncode
        return self.returncode


def _worker_config(**local_overrides):
    local = {
        "mode": "worker",
        "worker_timeout_seconds": 2,
        "worker_max_audio_bytes": 1024 * 1024,
    }
    local.update(local_overrides)
    return {"language": "en", "local": local}


def test_worker_returns_transcript_without_invoking_user_command(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        response_path = argv[argv.index("--response") + 1]
        return _FakeWorkerProcess(
            response_path=response_path,
            response={"success": True, "transcript": "hello from worker", "provider": "local"},
        )

    monkeypatch.setattr(stt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: _worker_config())
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)
    monkeypatch.setattr(stt, "_local_model", None)
    monkeypatch.setattr(stt, "_local_model_name", None)

    result = stt._transcribe_local(str(audio), "medium")

    assert result == {"success": True, "transcript": "hello from worker", "provider": "local"}
    assert captured["argv"][:3] == [stt.sys.executable, "-m", "tools.local_stt_worker"]
    assert captured["kwargs"]["shell"] is False
    assert "HERMES_LOCAL_STT_COMMAND" not in captured["argv"]
    assert all(isinstance(arg, str) for arg in captured["argv"])


def test_worker_nonzero_child_failure_is_returned_as_stt_error(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["process"] = _FakeWorkerProcess(returncode=17)
        return captured["process"]

    monkeypatch.setattr(stt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: _worker_config())
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)

    result = stt._transcribe_local(str(audio), "medium")

    assert result["success"] is False
    assert result["transcript"] == ""
    assert "exited with code 17" in result["error"]
    assert captured["process"].wait_calls == [2]


def test_worker_timeout_terminates_child_and_cleans_private_contract(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["request"] = Path(argv[argv.index("--request") + 1])
        captured["response"] = Path(argv[argv.index("--response") + 1])
        captured["process"] = _FakeWorkerProcess(timeout=True)
        return captured["process"]

    def fake_terminate(process):
        captured["terminated"] = process
        process.terminated = True
        process.returncode = -9

    monkeypatch.setattr(stt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stt, "_terminate_local_stt_worker_process_tree", fake_terminate)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: _worker_config())
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)

    result = stt._transcribe_local(str(audio), "medium")

    assert result["success"] is False
    assert "timed out after 2s" in result["error"]
    assert captured["terminated"] is captured["process"]
    assert captured["process"].terminated is True
    assert not captured["request"].exists()
    assert not captured["response"].exists()


def test_worker_mode_does_not_retain_model_object_in_parent(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    model_loader = MagicMock(name="WhisperModel")

    def fake_popen(argv, **kwargs):
        response_path = argv[argv.index("--response") + 1]
        return _FakeWorkerProcess(
            response_path=response_path,
            response={"success": True, "transcript": "released", "provider": "local"},
        )

    monkeypatch.setattr(stt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: _worker_config())
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)
    monkeypatch.setattr(stt, "_local_model", None)
    monkeypatch.setattr(stt, "_local_model_name", None)
    monkeypatch.setattr(stt, "_load_local_whisper_model", model_loader)

    result = stt._transcribe_local(str(audio), "medium")

    assert result["transcript"] == "released"
    assert stt._local_model is None
    assert stt._local_model_name is None
    model_loader.assert_not_called()


def test_worker_mode_releases_stale_in_process_model(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    stale_model = MagicMock(name="stale-model")
    monkeypatch.setattr(stt, "_local_model", stale_model)
    monkeypatch.setattr(stt, "_local_model_name", "medium")
    monkeypatch.setattr(stt, "_load_stt_config", lambda: _worker_config())
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)

    def fake_popen(argv, **kwargs):
        response_path = argv[argv.index("--response") + 1]
        return _FakeWorkerProcess(
            response_path=response_path,
            response={"success": True, "transcript": "released", "provider": "local"},
        )

    monkeypatch.setattr(stt.subprocess, "Popen", fake_popen)
    result = stt._transcribe_local(str(audio), "medium")

    assert result["success"] is True
    assert stt._local_model is None
    assert stt._local_model_name is None


def test_worker_admission_serializes_concurrent_model_initialization(monkeypatch, tmp_path):
    """Concurrent worker requests must not start independent model loads."""
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    first_worker_started = threading.Event()
    release_first_worker = threading.Event()
    started = []
    results = []

    def fake_run(request_path, response_path, timeout):
        started.append(threading.get_ident())
        if len(started) == 1:
            first_worker_started.set()
            assert release_first_worker.wait(timeout=2)
        return {"success": True, "transcript": "serialized", "provider": "local"}

    monkeypatch.setattr(stt, "_run_local_stt_worker_process", fake_run)
    monkeypatch.setattr(stt, "_local_stt_worker_admission_lock", threading.Lock())

    def transcribe():
        results.append(stt._transcribe_local_worker(str(audio), "medium", _worker_config()))

    first = threading.Thread(target=transcribe)
    second = threading.Thread(target=transcribe)
    first.start()
    assert first_worker_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert len(started) == 1
    release_first_worker.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(started) == 2
    assert results == [
        {"success": True, "transcript": "serialized", "provider": "local"},
        {"success": True, "transcript": "serialized", "provider": "local"},
    ]


def test_worker_input_size_is_bounded_before_child_spawn(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"12345")
    popen = MagicMock()
    monkeypatch.setattr(stt.subprocess, "Popen", popen)
    monkeypatch.setattr(
        stt,
        "_load_stt_config",
        lambda: _worker_config(worker_max_audio_bytes=4),
    )
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)

    result = stt._transcribe_local(str(audio), "medium")

    assert result["success"] is False
    assert "worker input exceeds" in result["error"]
    popen.assert_not_called()


def test_worker_limits_and_mode_have_safe_validated_defaults():
    assert stt.DEFAULT_LOCAL_MODEL == "medium"
    assert stt._get_local_stt_mode({}) == "worker"
    assert stt._get_local_stt_mode({"local": {"mode": "in_process"}}) == "in_process"
    assert stt._get_local_stt_mode({"local": {"mode": "shell"}}) == "worker"

    timeout, max_bytes = stt._get_local_stt_worker_limits(
        {"local": {"worker_timeout_seconds": "not-a-number", "worker_max_audio_bytes": -1}}
    )
    assert timeout == stt.DEFAULT_LOCAL_STT_WORKER_TIMEOUT_SECONDS
    assert max_bytes == stt.DEFAULT_LOCAL_STT_WORKER_MAX_AUDIO_BYTES
    assert timeout <= stt.MAX_LOCAL_STT_WORKER_TIMEOUT_SECONDS
    assert max_bytes <= stt.MAX_LOCAL_STT_WORKER_AUDIO_BYTES


def test_worker_module_writes_transcript_result(tmp_path, monkeypatch):
    """The child-side JSON contract returns only a bounded transcript envelope."""
    from tools import local_stt_worker as worker

    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text(
        json.dumps({"protocol_version": worker.PROTOCOL_VERSION}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker,
        "_transcribe",
        lambda payload: {
            "success": True,
            "transcript": "child transcript",
            "provider": "local",
        },
    )

    assert worker.run(request_path, response_path) == 0
    assert json.loads(response_path.read_text(encoding="utf-8")) == {
        "success": True,
        "transcript": "child transcript",
        "provider": "local",
    }


def test_worker_module_validation_failure_is_reported_without_shell(tmp_path):
    """Malformed requests fail closed and still produce a controlled response."""
    from tools import local_stt_worker as worker

    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text(json.dumps({"protocol_version": 999}), encoding="utf-8")

    assert worker.run(request_path, response_path) == 0
    result = json.loads(response_path.read_text(encoding="utf-8"))
    assert result["success"] is False
    assert "protocol version" in result["error"]


def test_worker_entrypoint_writes_transcript(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake audio")
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    request.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "input_path": str(audio),
                "model": "medium",
                "device": "cpu",
                "compute_type": "int8",
                "transcribe_kwargs": {"language": "en"},
                "local_config": {},
            }
        ),
        encoding="utf-8",
    )

    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([], object())
    monkeypatch.setattr(stt, "_load_local_whisper_model", lambda *args, **kwargs: fake_model)
    monkeypatch.setattr(stt, "_join_confident_segments", lambda segments, config: "hello from child")

    from tools import local_stt_worker as worker

    assert worker.main(["--request", str(request), "--response", str(response)]) == 0
    assert json.loads(response.read_text(encoding="utf-8")) == {
        "success": True,
        "transcript": "hello from child",
        "provider": "local",
    }
    fake_model.transcribe.assert_called_once_with(str(audio), language="en")
