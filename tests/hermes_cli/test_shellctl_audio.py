"""HTTP security, lifecycle, and concurrency tests for shellctl audio."""
from __future__ import annotations

import concurrent.futures
import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

_ASSET = Path(__file__).resolve().parents[2] / "hermes_cli/shellctl_assets/hermes-shellctl"


def _load():
    loader = importlib.machinery.SourceFileLoader("shellctl_audio_test", str(_ASSET))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def server():
    module = _load()
    module._TOKEN = "audio-token"
    module._OPEN_CAPTURE.update({
        "active": False, "out_wav": None, "process": None,
        "watchdog": None,
    })
    instance = module.ThreadingHTTPServer(("127.0.0.1", 0), module._Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, instance.server_port
    finally:
        module._discard_open_capture()
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _http(port, method, path, body=None, token="audio-token"):
    headers = {}
    if token is not None:
        headers["X-Shellctl-Token"] = token
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


class _Process:
    def __init__(self):
        self.terminated = threading.Event()
        self.killed = False

    def terminate(self):
        self.terminated.set()

    def wait(self, timeout):
        if not self.terminated.wait(timeout):
            raise TimeoutError

    def kill(self):
        self.killed = True
        self.terminated.set()


def _mock_recorder(module, monkeypatch, process=None):
    process = process or _Process()
    monkeypatch.setattr(module, "_which", lambda *args: "/usr/bin/arecord")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    return process


def test_ping_and_audio_share_authentication(server):
    _, port = server
    assert _http(port, "GET", "/ping", token=None)[0] == 401
    assert _http(port, "GET", "/ping", token="wrong")[0] == 401
    assert _http(port, "GET", "/ping")[0] == 200
    assert _http(port, "POST", "/record?secs=1", b"", token=None)[0] == 401


@pytest.mark.parametrize("value", ["abc", "1.5", "", "0", "121", "-2"])
def test_malformed_seconds_return_400(server, value):
    _, port = server
    status, payload = _http(port, "POST", "/record?secs=" + value, b"")
    assert status == 400
    assert "secs" in json.loads(payload)["error"]


@pytest.mark.parametrize("extension", ["py", "../wav", "wav/../../x", "exe"])
def test_play_rejects_extension_outside_allowlist(server, extension):
    _, port = server
    status, payload = _http(port, "POST", "/play?fmt=" + extension, b"audio")
    assert status == 400
    assert json.loads(payload)["error"] == "unsupported audio format"


def test_play_accepts_allowlisted_extension(server, monkeypatch):
    module, port = server
    observed = {}

    def play(path):
        observed["suffix"] = Path(path).suffix
        return True, "played"

    monkeypatch.setattr(module, "_play_audio", play)
    status, payload = _http(port, "POST", "/play?fmt=WAV", b"RIFFaudio")
    assert status == 200
    assert json.loads(payload)["ok"] is True
    assert observed["suffix"] == ".wav"


def test_concurrent_capture_allows_one_start_and_normal_stop(server, monkeypatch):
    module, port = server
    barrier = threading.Barrier(2)
    _mock_recorder(module, monkeypatch)

    def start():
        barrier.wait(timeout=5)
        return _http(port, "POST", "/record-start", b"")[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start), pool.submit(start)]
        statuses = sorted(future.result() for future in futures)
    assert statuses == [200, 409]

    Path(module._OPEN_CAPTURE["out_wav"]).write_bytes(b"RIFFaudio")
    assert _http(port, "POST", "/record-stop", b"") == (200, b"RIFFaudio")
    assert module._OPEN_CAPTURE["active"] is False


def test_watchdog_expires_and_removes_capture(server, monkeypatch):
    module, port = server
    process = _mock_recorder(module, monkeypatch)
    module._MAX_OPEN_CAPTURE_SECS = 0.03
    assert _http(port, "POST", "/record-start", b"")[0] == 200
    output = Path(module._OPEN_CAPTURE["out_wav"])
    assert output.exists()
    assert process.terminated.wait(2)
    deadline = time.monotonic() + 2
    while output.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert module._OPEN_CAPTURE["active"] is False
    assert not output.exists()


def test_capture_size_cap_returns_controlled_error(server, monkeypatch):
    module, port = server
    module._MAX_AUDIO_BYTES = 8
    _mock_recorder(module, monkeypatch)
    assert _http(port, "POST", "/record-start", b"")[0] == 200
    output = Path(module._OPEN_CAPTURE["out_wav"])
    output.write_bytes(b"RIFF" + b"x" * 8)
    status, payload = _http(port, "POST", "/record-stop", b"")
    assert status == 413
    assert json.loads(payload)["error"] == "capture exceeds 8 byte limit"
    assert not output.exists()


def test_concurrent_stops_claim_capture_once(server, monkeypatch):
    module, port = server
    _mock_recorder(module, monkeypatch)
    assert _http(port, "POST", "/record-start", b"")[0] == 200
    Path(module._OPEN_CAPTURE["out_wav"]).write_bytes(b"RIFFaudio")
    barrier = threading.Barrier(2)

    def stop():
        barrier.wait(timeout=5)
        return _http(port, "POST", "/record-stop", b"")[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(stop), pool.submit(stop)]
        statuses = sorted(future.result() for future in futures)
    assert statuses == [200, 409]


def test_daemon_shutdown_discards_active_capture(tmp_path, monkeypatch):
    module = _load()
    process = _Process()
    output = tmp_path / "open.wav"
    output.write_bytes(b"RIFFaudio")
    module._OPEN_CAPTURE.update({
        "active": True, "out_wav": str(output), "process": process,
        "watchdog": None,
    })

    class _Server:
        def serve_forever(self):
            return None

        def server_close(self):
            pass

    monkeypatch.setattr(module, "ThreadingHTTPServer", lambda *args: _Server())
    args = SimpleNamespace(
        token="audio-token", token_file="", download_dir="",
        allowed_root="", max_open_capture_secs=300,
        host="127.0.0.1", port=0,
    )
    assert module.cmd_daemon(args) == 0
    assert process.terminated.is_set()
    assert module._OPEN_CAPTURE["active"] is False
    assert not output.exists()


def test_failed_start_response_discards_capture(monkeypatch):
    module = _load()
    process = _mock_recorder(module, monkeypatch)
    handler = object.__new__(module._Handler)
    handler.path = "/record-start"
    handler._auth_ok = lambda: True
    handler._send = lambda *args, **kwargs: False
    assert handler.do_POST() is False
    assert process.terminated.is_set()
    assert module._OPEN_CAPTURE["active"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal lifecycle test")
def test_sigterm_shutdown_reaps_real_recorder(tmp_path):
    recorder = tmp_path / "arecord"
    pid_file = tmp_path / "recorder.pid"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "open(os.environ['RECORDER_PID'], 'w').write(str(os.getpid()))\n"
        "open(sys.argv[-1], 'wb').write(b'RIFFaudio')\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env["RECORDER_PID"] = str(pid_file)
    daemon = subprocess.Popen(
        [
            sys.executable, str(_ASSET), "daemon", "--port", str(port),
            "--token", "audio-token", "--max-open-capture-secs", "300",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _http(port, "GET", "/ping")[0] == 200:
                    break
            except OSError:
                time.sleep(0.02)
        else:
            pytest.fail("shellctl daemon did not start")
        assert _http(port, "POST", "/record-start", b"")[0] == 200
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        recorder_pid = int(pid_file.read_text(encoding="utf-8"))
        daemon.terminate()
        assert daemon.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(recorder_pid, 0)
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)


def test_malformed_audio_content_length_returns_400(server):
    _, port = server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("POST", "/play?fmt=wav")
    connection.putheader("X-Shellctl-Token", "audio-token")
    connection.putheader("Content-Length", "not-a-number")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 400
    assert json.loads(response.read())["error"] == "invalid Content-Length"
    connection.close()


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_host_bridge_routes_tts_to_local_play(monkeypatch, capsys):
    import argparse
    import base64

    bridge_path = _ASSET.with_name("hermes-shellbridge")
    loader = importlib.machinery.SourceFileLoader("shellbridge_audio_test", str(bridge_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    bridge = importlib.util.module_from_spec(spec)
    loader.exec_module(bridge)
    audio = b"ID3audio"
    encoded = base64.b64encode(audio).decode()
    calls = {}

    def dash(*args, **kwargs):
        return _Response(
            json.dumps({"data_url": "data:audio/mp3;base64," + encoded}).encode()
        )

    def client(method, path, body=None, **kwargs):
        calls["client"] = method, path, body
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(bridge, "_dash_req", dash)
    monkeypatch.setattr(bridge, "_client_req", client)
    assert bridge.cmd_say(argparse.Namespace(text="Hello **world**")) == 0
    assert json.loads(capsys.readouterr().out)["played"] is True
    assert calls["client"] == ("POST", "/play?fmt=mp3", audio)
    assert Path(bridge.IMAGES_DIR).parent == bridge._HERMES_HOME


def test_host_bridge_bounded_audio_read_rejects_large_response():
    bridge_path = _ASSET.with_name("hermes-shellbridge")
    loader = importlib.machinery.SourceFileLoader("shellbridge_size_test", str(bridge_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    bridge = importlib.util.module_from_spec(spec)
    loader.exec_module(bridge)
    setattr(bridge, "_MAX_AUDIO_BYTES", 4)
    with pytest.raises(ValueError, match="4 byte limit"):
        bridge._read_audio_response(_Response(b"12345"))
