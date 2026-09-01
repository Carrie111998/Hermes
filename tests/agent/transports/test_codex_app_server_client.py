import threading
import time
import queue
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
    client._id_lock = threading.Lock()
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


def test_request_ids_are_unique_under_concurrent_allocation():
    client = _client_with_stdin(_BlockingStdin())
    results: list[int] = []
    result_lock = threading.Lock()

    def allocate() -> None:
        value = client._take_id()
        with result_lock:
            results.append(value)

    threads = [threading.Thread(target=allocate) for _ in range(64)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    assert sorted(results) == list(range(1, 65))


def test_notification_queue_keeps_newest_terminal_event(caplog):
    client = _client_with_stdin(_BlockingStdin())
    client._notifications = queue.Queue(maxsize=2)
    client._server_requests = queue.Queue(maxsize=1)

    client._dispatch({"method": "item/reasoning/delta"})
    client._dispatch({"method": "item/agentMessage/delta"})
    client._dispatch({"method": "turn/completed"})

    assert client._notifications.get_nowait()["method"] == "item/agentMessage/delta"
    assert client._notifications.get_nowait()["method"] == "turn/completed"
    assert "dropped oldest notification" in caplog.text


def test_server_request_queue_overflow_is_nonblocking_and_fail_closed(caplog):
    client = _client_with_stdin(_BlockingStdin())
    client._notifications = queue.Queue(maxsize=1)
    client._server_requests = queue.Queue(maxsize=1)

    client._dispatch({"id": "first", "method": "approval/one"})
    client._dispatch({"id": "second", "method": "approval/two"})

    assert client._server_requests.get_nowait()["id"] == "first"
    assert "server-request queue full" in caplog.text
