"""Regression tests for #72071 — a stalled secret-source fetch must never
hang the CLI.

``agent.secret_sources.registry._fetch_with_timeout`` enforces the
per-source wall-clock budget that protects every CLI entrypoint (e.g.
``hermes gateway status``) from a secrets backend that blocks.  Two
properties matter:

* the budget actually returns a ``TIMEOUT`` FetchResult promptly, and
* the fetch runs on a *daemon* thread, so a fetch stuck in a hung
  download/subprocess cannot block interpreter shutdown.  (The previous
  ThreadPoolExecutor-based implementation used non-daemon workers that
  ``concurrent.futures.thread`` joins at exit, so the process hung
  forever even after the timeout fired.)
"""

import threading
import time
from pathlib import Path
from typing import Optional

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource
from agent.secret_sources.registry import _fetch_with_timeout


class _StallingSource(SecretSource):
    """Simulates a fetch stuck in an unresponsive download/subprocess."""

    name = "stalling"
    label = "Stalling Source"
    shape = "bulk"

    def __init__(self, release: threading.Event):
        self._release = release
        self.thread: Optional[threading.Thread] = None

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        self.thread = threading.current_thread()
        # Outlive the 0.2s budget by a wide margin unless released.
        self._release.wait(30)
        return FetchResult()

    def fetch_timeout_seconds(self, cfg: dict) -> float:
        return 0.2


class _FastSource(SecretSource):
    name = "fast"
    label = "Fast Source"
    shape = "bulk"

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        result = FetchResult()
        result.secrets = {"FAST_TOKEN": "value"}
        return result


class _RaisingSource(SecretSource):
    name = "raising"
    label = "Raising Source"
    shape = "bulk"

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        raise RuntimeError("boom")


class _WrongTypeSource(SecretSource):
    name = "wrongtype"
    label = "Wrong Type Source"
    shape = "bulk"

    def fetch(self, cfg: dict, home_path: Path):  # type: ignore[override]
        return {"not": "a FetchResult"}


def test_timeout_returns_promptly_without_waiting_for_fetch(tmp_path):
    release = threading.Event()
    source = _StallingSource(release)
    try:
        start = time.monotonic()
        result = _fetch_with_timeout(source, {}, tmp_path)
        elapsed = time.monotonic() - start

        assert result.error_kind == ErrorKind.TIMEOUT
        assert "budget" in (result.error or "")
        # Must come back around the 0.2s budget, not the 30s stall.
        assert elapsed < 5
    finally:
        release.set()


def test_stalled_fetch_runs_on_daemon_thread(tmp_path):
    """The worker must be a daemon so a stuck fetch can't block exit (#72071)."""
    release = threading.Event()
    source = _StallingSource(release)
    try:
        _fetch_with_timeout(source, {}, tmp_path)
        assert source.thread is not None
        assert source.thread is not threading.main_thread()
        assert source.thread.daemon
    finally:
        release.set()
        if source.thread is not None:
            source.thread.join(timeout=5)


def test_fast_fetch_result_passes_through(tmp_path):
    result = _fetch_with_timeout(_FastSource(), {}, tmp_path)
    assert result.ok
    assert result.secrets == {"FAST_TOKEN": "value"}


def test_fetch_exception_contained_as_internal_error(tmp_path):
    result = _fetch_with_timeout(_RaisingSource(), {}, tmp_path)
    assert result.error_kind == ErrorKind.INTERNAL
    assert "RuntimeError" in (result.error or "")


def test_non_fetchresult_return_contained_as_internal_error(tmp_path):
    result = _fetch_with_timeout(_WrongTypeSource(), {}, tmp_path)
    assert result.error_kind == ErrorKind.INTERNAL
    assert "dict" in (result.error or "")
