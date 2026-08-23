"""Concurrency regression tests for the shared auth-store lock."""

from __future__ import annotations

import threading

from hermes_cli.auth import _auth_store_lock


def test_concurrent_windows_lock_initialization_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    concurrency = 40
    barrier = threading.Barrier(concurrency)
    errors: list[BaseException] = []
    entered = 0
    entered_guard = threading.Lock()

    def acquire() -> None:
        nonlocal entered
        try:
            barrier.wait(timeout=10)
            with _auth_store_lock(timeout_seconds=10):
                with entered_guard:
                    entered += 1
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert entered == concurrency
