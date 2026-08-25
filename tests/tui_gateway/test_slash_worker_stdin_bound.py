"""_SlashWorker.run must bound its stdin write.

run() holds _lock across proc.stdin.write(). A command larger than the pipe
buffer against a busy child (still executing the previous command) blocked
that write forever while holding the lock - every later slash command from
any thread queued behind it, and the queue-wait timeout was never reached
because the call never got past the write. The write is now offloaded to a
helper thread bounded by the same timeout.
"""
import queue
import threading
import types

import pytest

from tui_gateway import server


class _BlockingStdin:
    """stdin whose write() blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.written = []

    def write(self, payload):
        self.written.append(payload)
        self.release.wait(timeout=10)

    def flush(self):
        pass


def _make_worker(stdin):
    worker = object.__new__(server._SlashWorker)
    worker.proc = types.SimpleNamespace(
        poll=lambda: None,  # "running"
        stdin=stdin,
        stdout=None,
        stderr=None,
    )
    worker._lock = threading.Lock()
    worker._seq = 0
    worker.stdout_queue = queue.Queue()
    worker.stderr_tail = []
    return worker


def test_run_raises_timeout_when_stdin_write_blocks(monkeypatch):
    stdin = _BlockingStdin()
    worker = _make_worker(stdin)
    monkeypatch.setattr(server, "_SLASH_WORKER_TIMEOUT_S", 0.2)

    with pytest.raises(RuntimeError, match="stdin write timed out"):
        worker.run("huge command")

    # Lock must be released: a second call proceeds (and times out again
    # instead of deadlocking behind the first).
    with pytest.raises(RuntimeError):
        worker.run("second")

    stdin.release.set()


def test_run_returns_output_when_write_and_response_succeed(monkeypatch):
    class _EchoStdin(_BlockingStdin):
        def write(self, payload):
            self.written.append(payload)
            # Simulate the child answering immediately.

    stdin = _EchoStdin()
    worker = _make_worker(stdin)

    def responder():
        worker.stdout_queue.put({"id": 1, "ok": True, "output": "done"})

    monkeypatch.setattr(
        server, "_SLASH_WORKER_TIMEOUT_S", 2.0
    )
    # Answer the first command as soon as the payload lands.
    orig_start = threading.Thread

    def _spawn_then_answer(target, daemon=False, name=None):
        t = orig_start(target=target, daemon=daemon, name=name)
        responder()
        return t

    monkeypatch.setattr(threading, "Thread", _spawn_then_answer)
    assert worker.run("cmd") == "done"
