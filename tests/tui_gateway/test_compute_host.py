import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hermes_constants import mark_named_profile_deleted
from tui_gateway.compute_host import ComputeHost, HostSession
from tui_gateway.server import (
    _open_profile_session_db,
    _require_existing_profile_home,
)


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


@pytest.mark.parametrize("cached", [False, True])
def test_compute_host_rejects_deleted_profile_home_before_agent_build(
    tmp_path, cached
):
    profile_home = tmp_path / "profiles" / "deleted"
    sid = "stale-profile"

    class _Server:
        _sessions = (
            {
                sid: {
                    "session_key": sid,
                    "profile_home": str(profile_home),
                }
            }
            if cached
            else {}
        )
        _require_existing_profile_home = staticmethod(_require_existing_profile_home)

        @staticmethod
        def _make_agent(*_args, **_kwargs):
            raise AssertionError("agent construction must not run")

    host = ComputeHost(heartbeat_secs=0)
    try:
        with pytest.raises(RuntimeError, match="profile home no longer exists"):
            host._ensure_server_session(
                _Server,
                {
                    "sid": sid,
                    "session_key": sid,
                    "profile_home": str(profile_home),
                },
            )
    finally:
        host.close()

    assert not profile_home.exists()


@pytest.mark.parametrize("cached", [False, True])
def test_compute_host_rejects_tombstoned_profile_before_agent_build(
    tmp_path, cached
):
    profile_home = tmp_path / ".hermes" / "profiles" / "deleted"
    profile_home.mkdir(parents=True)
    mark_named_profile_deleted(profile_home)
    sid = "stale-profile"

    class _Server:
        _sessions = (
            {
                sid: {
                    "session_key": sid,
                    "profile_home": str(profile_home),
                }
            }
            if cached
            else {}
        )
        _require_existing_profile_home = staticmethod(_require_existing_profile_home)

        @staticmethod
        def _open_profile_session_db(*_args, **_kwargs):
            raise AssertionError("SessionDB construction must not run")

        @staticmethod
        def _make_agent(*_args, **_kwargs):
            raise AssertionError("agent construction must not run")

    host = ComputeHost(heartbeat_secs=0)
    try:
        with pytest.raises(RuntimeError, match="profile home no longer exists"):
            host._ensure_server_session(
                _Server,
                {
                    "sid": sid,
                    "session_key": sid,
                    "profile_home": str(profile_home),
                },
            )
    finally:
        host.close()

    assert list(profile_home.iterdir()) == []


def test_profile_home_guard_accepts_existing_directory(tmp_path):
    profile_home = tmp_path / "profiles" / "live"
    profile_home.mkdir(parents=True)

    assert _require_existing_profile_home(profile_home) == profile_home


def test_profile_db_open_does_not_create_missing_profile_home(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profiles" / "deleted"
    monkeypatch.setattr(
        "hermes_state.SessionDB",
        lambda **_kwargs: pytest.fail("SessionDB construction must not run"),
    )

    with pytest.raises(RuntimeError, match="profile home no longer exists"):
        _open_profile_session_db(profile_home)

    assert not profile_home.exists()
