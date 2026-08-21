import json
import os
import queue
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from tui_gateway.compute_host import ComputeHost, HostSession


def test_compute_host_fallback_preserves_empty_attribution(monkeypatch):
    host = ComputeHost(
        stdout=types.SimpleNamespace(write=lambda *_: None, flush=lambda: None)
    )
    agent = types.SimpleNamespace()
    server = types.SimpleNamespace(
        _sessions={},
        _make_agent=lambda *_args, **_kwargs: agent,
        _transfer_db_to_agent=lambda *_args: False,
        _init_session=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        _load_show_reasoning=lambda: False,
        _load_tool_progress_mode=lambda: "compact",
        _current_profile_name=lambda: "default",
        _sanitize_client_source=lambda source: source,
    )
    monkeypatch.setattr("tui_gateway.transport.bind_transport", lambda _transport: None)
    monkeypatch.setattr("tui_gateway.transport.reset_transport", lambda _token: None)

    session = host._ensure_server_session(
        server,
        {"sid": "s1", "session_key": "durable-s1", "cwd": ""},
    )

    assert session["cwd"] == ""
    assert session["profile_name"] == ""
    host.close()


def test_compute_host_forwards_profile_to_session_registration(monkeypatch):
    host = ComputeHost(
        stdout=types.SimpleNamespace(write=lambda *_: None, flush=lambda: None),
        heartbeat_secs=0,
    )
    captured = {}
    agent = types.SimpleNamespace()

    def _init_session(*_args, **kwargs):
        captured.update(kwargs)
        server._sessions["s1"] = {"agent": agent, "session_key": "durable-s1"}

    server = types.SimpleNamespace(
        _sessions={},
        _make_agent=lambda *_args, **_kwargs: agent,
        _transfer_db_to_agent=lambda *_args: False,
        _init_session=_init_session,
    )
    monkeypatch.setattr("tui_gateway.transport.bind_transport", lambda _transport: None)
    monkeypatch.setattr("tui_gateway.transport.reset_transport", lambda _token: None)

    host._ensure_server_session(
        server,
        {
            "sid": "s1",
            "session_key": "durable-s1",
            "cwd": "/workspace",
            "profile_name": "reviewer",
        },
    )

    assert captured["cwd"] == "/workspace"
    assert captured["profile_name"] == "reviewer"
    host.close()


def test_compute_host_forwards_explicit_empty_attribution(monkeypatch):
    host = ComputeHost(
        stdout=types.SimpleNamespace(write=lambda *_: None, flush=lambda: None),
        heartbeat_secs=0,
    )
    captured = {}
    agent = types.SimpleNamespace()

    def _init_session(*_args, **kwargs):
        captured.update(kwargs)
        server._sessions["s1"] = {
            "agent": agent,
            "session_key": "durable-s1",
            "profile_home": "/stale/profile-home",
            "profile_name": "stale-profile",
        }

    server = types.SimpleNamespace(
        _sessions={},
        _make_agent=lambda *_args, **_kwargs: agent,
        _transfer_db_to_agent=lambda *_args: False,
        _init_session=_init_session,
    )
    monkeypatch.setattr("tui_gateway.transport.bind_transport", lambda _transport: None)
    monkeypatch.setattr("tui_gateway.transport.reset_transport", lambda _token: None)

    host._ensure_server_session(
        server,
        {
            "sid": "s1",
            "session_key": "durable-s1",
            "cwd": "",
            "profile_home": "",
            "profile_name": "",
        },
    )

    assert captured["cwd"] == ""
    assert captured["profile_name"] is None
    assert server._sessions["s1"]["profile_home"] is None
    assert server._sessions["s1"]["profile_name"] == ""
    host.close()


def test_compute_host_cached_session_clears_explicit_empty_attribution():
    host = ComputeHost(
        stdout=types.SimpleNamespace(write=lambda *_: None, flush=lambda: None),
        heartbeat_secs=0,
    )
    session = {
        "session_key": "durable-s1",
        "cwd": "/stale/workspace",
        "profile_home": "/stale/profile-home",
        "profile_name": "stale-profile",
    }
    server = types.SimpleNamespace(_sessions={"s1": session})

    result = host._ensure_server_session(
        server,
        {
            "sid": "s1",
            "session_key": "durable-s1",
            "cwd": "",
            "profile_home": "",
            "profile_name": "",
        },
    )

    assert result["cwd"] == ""
    assert result["profile_home"] is None
    assert result["profile_name"] == ""
    host.close()


def _stdout_queue(proc: subprocess.Popen) -> queue.Queue[dict]:
    out: queue.Queue[dict] = queue.Queue()
    assert proc.stdout is not None

    def drain() -> None:
        for line in proc.stdout or []:
            out.put(json.loads(line))

    threading.Thread(target=drain, daemon=True).start()
    return out


def _read_json_line(out: queue.Queue[dict], timeout: float = 2.0) -> dict:
    try:
        return out.get(timeout=timeout)
    except queue.Empty as exc:
        raise AssertionError("timed out waiting for compute host JSON") from exc


def test_compute_host_line_json_seed_turn_interrupt():
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tui_gateway.compute_host"],
        cwd=str(repo),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None
    out = _stdout_queue(proc)
    try:
        hello = _read_json_line(out)
        assert hello["type"] == "hello"
        assert hello["host_pid"] == proc.pid

        proc.stdin.write(json.dumps({"type": "session.seed", "sid": "s1", "request_id": "seed"}) + "\n")
        proc.stdin.flush()
        assert _read_json_line(out)["type"] == "session.seeded"

        proc.stdin.write(
            json.dumps(
                {
                    "type": "turn.start",
                    "sid": "s1",
                    "request_id": "turn",
                    "prompt": "hello",
                    "delta_count": 3,
                    "delay_s": 0,
                }
            )
            + "\n"
        )
        proc.stdin.flush()

        seen = []
        while True:
            frame = _read_json_line(out)
            seen.append(frame["type"])
            if frame["type"] == "turn.end":
                assert frame["history_version"] == 1
                assert frame["message_count"] == 2
                break
        assert seen.count("delta") == 3

        proc.stdin.write(json.dumps({"type": "shutdown", "request_id": "stop"}) + "\n")
        proc.stdin.flush()
        assert _read_json_line(out)["type"] == "shutdown.ack"
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("kind", ["legacy", "hard-only", "dynamic-getattr"])
def test_compute_host_interrupt_uses_explicit_stop_compatibility(kind):
    calls = []

    class _Legacy:
        def interrupt(self):
            calls.append("legacy")

    class _HardOnly:
        def hard_interrupt(self):
            calls.append("hard")

    class _Dynamic:
        def interrupt(self):
            calls.append("legacy")

        def __getattr__(self, name):
            if name == "hard_interrupt":
                return lambda: calls.append("fabricated-hard")
            raise AttributeError(name)

    agent = {
        "legacy": _Legacy(),
        "hard-only": _HardOnly(),
        "dynamic-getattr": _Dynamic(),
    }[kind]
    host = ComputeHost(heartbeat_secs=0)
    host._sessions["s1"] = HostSession(sid="s1", agent=agent)
    emitted = []
    host.emit = emitted.append
    try:
        host._handle_interrupt({"sid": "s1", "request_id": "stop"})
    finally:
        host.close()

    assert calls == ["hard" if kind == "hard-only" else "legacy"]
    assert emitted[-1]["applied"] is True
