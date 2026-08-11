"""Regression coverage for thread-safe transport discovery."""

import builtins
import threading

import agent.transports as transports


def test_discovery_flag_is_set_only_after_imports_complete(monkeypatch):
    """Concurrent callers must not observe discovery as complete mid-import."""
    original_discovered = transports._discovered
    entered_import = threading.Event()
    release_import = threading.Event()
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "agent.transports.anthropic":
            entered_import.set()
            assert release_import.wait(timeout=2), "timed out waiting to release import"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    transports._discovered = False

    worker = threading.Thread(target=transports._discover_transports)
    try:
        worker.start()
        assert entered_import.wait(timeout=2), "transport discovery never started"

        # The old implementation set this flag before importing transports,
        # allowing another thread to skip discovery and read a partial registry.
        assert transports._discovered is False

        release_import.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert transports._discovered is True
    finally:
        release_import.set()
        worker.join(timeout=2)
        transports._discovered = original_discovered
