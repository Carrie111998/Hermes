import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tui_gateway.compute_host import ComputeHost, HostSession


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


def test_compute_host_binds_exact_frame_identity_before_agent_build(monkeypatch, tmp_path):
    seen = {}
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)

    class FakeDB:
        def close(self):
            seen["db_closed"] = True

    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: FakeDB())

    class FakeServer:
        _sessions = {}

        @staticmethod
        def _set_session_context(key, **kwargs):
            seen["context"] = (key, kwargs)
            return ["token"]

        @staticmethod
        def _clear_session_context(tokens):
            seen["cleared"] = tokens

        @staticmethod
        def _make_agent(sid, key, **kwargs):
            seen["agent"] = (sid, key, kwargs)
            return object()

        @classmethod
        def _init_session(cls, sid, key, agent, history, **kwargs):
            seen["init_kwargs"] = kwargs
            cls._sessions[sid] = {
                "agent": agent,
                "session_key": key,
                "history": history,
                "cwd": kwargs.get("cwd"),
            }

    frame = {
        "sid": "ui-compute",
        "session_key": "durable-compute",
        "cwd": "/work/compute",
        "profile_home": str(profile_home),
        "source": "tui",
        "history": [],
    }
    host = ComputeHost(heartbeat_secs=0)
    try:
        host._ensure_server_session(FakeServer, frame)
    finally:
        host.close()

    assert seen["context"] == (
        "durable-compute",
        {
            "ui_session_id": "ui-compute",
            "session": {
                "session_key": "durable-compute",
                "cwd": "/work/compute",
                "profile_home": str(profile_home),
                "source": "tui",
            },
        },
    )
    assert seen["init_kwargs"]["profile_home"] == str(profile_home)
    assert seen["agent"][0:2] == ("ui-compute", "durable-compute")
    assert seen["cleared"] == ["token"]
