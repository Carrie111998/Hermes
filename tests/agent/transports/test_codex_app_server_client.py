import threading
import time
from types import SimpleNamespace

import pytest

from agent.transports.codex_app_server import CodexAppServerClient


class _BlockingStdin:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.release = threading.Event()
        self.poisoned = False
        self.closed = False
        self.frames: list[bytes] = []

    def poison(self) -> None:
        self.poisoned = True
        self.release.set()

    def write(self, payload: bytes) -> int:
        self.started.set()
        try:
            self.release.wait(timeout=2.0)
            if self.poisoned:
                raise BrokenPipeError("transport terminated")
            self.frames.append(payload)
            return len(payload)
        finally:
            self.finished.set()

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self.release.set()


class _Process:
    def __init__(self, stdin: _BlockingStdin) -> None:
        self.stdin = stdin
        self.returncode = None
        self.killed = False
        self.wait_calls = 0

    def terminate(self) -> None:
        # Deliberately does not stop the child. The timeout path must escalate
        # directly to kill rather than trusting cooperative termination.
        return None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdin.poison()

    def wait(self, timeout=None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise TimeoutError("still running")
        return self.returncode

    def poll(self):
        return self.returncode


def _client_with_stdin(stdin: _BlockingStdin) -> CodexAppServerClient:
    client = object.__new__(CodexAppServerClient)
    client._closed = False
    client._poisoned = False
    setattr(client, "_proc", _Process(stdin))
    client._send_lock = threading.Lock()
    client._pending = {}
    client._pending_lock = threading.Lock()
    client._next_id = 1
    return client


def test_request_deadline_poison_kills_reaps_and_blocks_ghost_frame():
    stdin = _BlockingStdin()
    client = _client_with_stdin(stdin)

    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="stdin write timed out"):
        client.request("turn/start", {"threadId": "thread"}, timeout=0.05)

    elapsed = time.monotonic() - started_at
    assert stdin.started.is_set()
    assert stdin.finished.wait(timeout=0.25)
    assert elapsed < 0.35
    assert client._pending == {}
    assert client._closed is True
    assert client.poisoned is True
    assert getattr(client._proc, "killed", False) is True
    assert getattr(client._proc, "wait_calls", 0) >= 1
    assert client.is_alive() is False
    assert stdin.frames == []

    with pytest.raises(RuntimeError, match="client is closed"):
        client.notify("turn/steer", {"text": "must not be sent"})
