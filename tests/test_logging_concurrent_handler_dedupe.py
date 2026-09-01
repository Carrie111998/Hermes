"""Concurrent logging init must not duplicate file handlers (#100261).

`_add_rotating_handler` deduped by resolved path with NO lock held, while the
append happened later inside `_register_queued_handler` under
`_queue_state_lock`. Two threads initialising logging concurrently could both
pass the check and both append a handler for the same file — the QueueListener
was rebuilt with both and every record was written twice for the life of the
process.
"""

import logging
import threading
from pathlib import Path

import pytest

import hermes_logging


@pytest.fixture(autouse=True)
def _isolate_queue_state(monkeypatch):
    """Give each test a private handler list / listener so the real process
    logging state is never mutated."""
    monkeypatch.setattr(hermes_logging, "_queued_file_handlers", [])
    monkeypatch.setattr(hermes_logging, "_log_queue", None)
    monkeypatch.setattr(hermes_logging, "_queue_listener", None)
    monkeypatch.setattr(hermes_logging, "_queue_atexit_registered", True)
    yield
    listener = hermes_logging._queue_listener
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass
    for h in list(hermes_logging._queued_file_handlers):
        try:
            h.close()
        except Exception:
            pass


def _add(path: Path):
    hermes_logging._add_rotating_handler(
        logging.getLogger(f"t.{path.stem}"),
        path,
        level=logging.INFO,
        max_bytes=1024,
        backup_count=1,
        formatter=logging.Formatter("%(message)s"),
    )


def _resolved_paths():
    return [
        Path(getattr(h, "baseFilename", "")).resolve()
        for h in hermes_logging._queued_file_handlers
    ]


def test_sequential_add_is_idempotent(tmp_path):
    """Baseline: the documented idempotence still holds."""
    log = tmp_path / "agent.log"
    _add(log)
    _add(log)
    assert len(hermes_logging._queued_file_handlers) == 1


def test_concurrent_add_registers_one_handler_per_path(tmp_path):
    """#100261: two threads racing the same path must yield ONE handler.

    A Barrier forces both threads past the (unlocked) pre-check before
    either registers — the exact interleaving from the report.
    """
    log = tmp_path / "agent.log"
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            _add(log)
        except Exception as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    resolved = _resolved_paths()
    assert len(hermes_logging._queued_file_handlers) == 1, (
        f"expected one handler for {log}, got {len(resolved)}: {resolved}"
    )


def test_many_concurrent_adds_still_one_handler(tmp_path):
    """Widen the race: 8 threads, one path, still exactly one handler."""
    log = tmp_path / "agent.log"
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(timeout=5)
        _add(log)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(hermes_logging._queued_file_handlers) == 1


def test_distinct_paths_each_register(tmp_path):
    """The dedupe must not over-collapse: different files still get handlers."""
    a = tmp_path / "agent.log"
    b = tmp_path / "gateway.log"
    barrier = threading.Barrier(2)

    def worker(p):
        barrier.wait(timeout=5)
        _add(p)

    threads = [
        threading.Thread(target=worker, args=(a,)),
        threading.Thread(target=worker, args=(b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(hermes_logging._queued_file_handlers) == 2
    assert set(_resolved_paths()) == {a.resolve(), b.resolve()}


def test_losing_racer_does_not_leak_its_file_handle(tmp_path):
    """The dropped handler is closed, so its just-opened fd isn't leaked."""
    log = tmp_path / "agent.log"
    closed = []

    real_cls = hermes_logging._ManagedRotatingFileHandler

    class _TrackingHandler(real_cls):
        def close(self):
            closed.append(self)
            super().close()

    hermes_logging._ManagedRotatingFileHandler = _TrackingHandler
    try:
        _add(log)          # first wins
        _add(log)          # second is dropped inside the lock
    finally:
        hermes_logging._ManagedRotatingFileHandler = real_cls

    assert len(hermes_logging._queued_file_handlers) == 1
    # The pre-check short-circuits the sequential case before a handler is
    # built, so nothing to close; the guarantee is simply "no duplicate".
    assert len(closed) == 0


def test_register_returns_false_when_path_already_covered(tmp_path):
    """_register_queued_handler reports whether it took the handler."""
    log = tmp_path / "agent.log"
    _add(log)
    assert len(hermes_logging._queued_file_handlers) == 1

    dup = hermes_logging._ManagedRotatingFileHandler(
        str(log), maxBytes=1024, backupCount=1, encoding="utf-8"
    )
    took = hermes_logging._register_queued_handler(
        dup, dedupe_path=log.resolve()
    )
    assert took is False
    assert len(hermes_logging._queued_file_handlers) == 1
