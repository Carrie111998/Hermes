from __future__ import annotations

import threading
import time

from agent.speculative_compression import (
    SpeculativeCompressionManager,
    SpeculativeSnapshot,
    capture_snapshot,
)


class Compressor:
    def _protect_head_size(self, messages):
        return 0

    def _find_tail_cut_by_tokens(self, messages, _head_end):
        return len(messages) - 1

    def _align_boundary_forward(self, _messages, cut):
        return cut


def _snapshot(session_id="session-1"):
    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "tail"},
    ]
    return capture_snapshot(messages, Compressor(), 100, session_id)


class Worker:
    def __init__(self, snapshot, started=None, release=None):
        self.snapshot = snapshot
        self.started = started
        self.release = release

    def compress(self, messages, **_kwargs):
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(2)
        return [
            {"role": "user", "content": "summary"},
            *messages[self.snapshot.cut_index :],
        ]


def test_manager_coalesces_single_flight_and_returns_one_candidate():
    manager = SpeculativeCompressionManager(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    snapshot = _snapshot()
    calls = []

    def factory():
        calls.append(1)
        return Worker(snapshot, started, release)

    try:
        assert manager.maybe_start("session-1", snapshot, factory) == "started"
        assert started.wait(1)
        assert all(thread.daemon for thread in manager._executor._threads)
        assert manager.maybe_start("session-1", snapshot, factory) == "coalesced"
        release.set()
        candidate = manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=2
        )
        assert candidate is not None
        assert len(calls) == 1
    finally:
        release.set()
        manager.shutdown()


def _snapshot_messages():
    return [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "tail"},
    ]


def test_manager_discards_stale_candidate():
    manager = SpeculativeCompressionManager(
        max_workers=1, clock=lambda: time.monotonic() + 10_000
    )
    snapshot = _snapshot()
    try:
        assert (
            manager.maybe_start(
                "session-1", snapshot, lambda: Worker(snapshot), max_age_seconds=1
            )
            == "started"
        )
        assert (
            manager.take_matching_candidate(
                "session-1", _snapshot_messages(), wait_seconds=1, max_age_seconds=1
            )
            is None
        )
    finally:
        manager.shutdown()


def test_manager_cancellation_prevents_late_candidate():
    manager = SpeculativeCompressionManager(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    snapshot = _snapshot()
    try:
        assert (
            manager.maybe_start(
                "session-1", snapshot, lambda: Worker(snapshot, started, release)
            )
            == "started"
        )
        assert started.wait(1)
        manager.invalidate_session("session-1")
        release.set()
        time.sleep(0.05)
        assert (
            manager.take_matching_candidate("session-1", _snapshot_messages()) is None
        )
    finally:
        release.set()
        manager.shutdown()


def test_manager_reruns_coalesced_snapshot_with_new_fingerprint():
    """A coalesced snapshot whose prefix changed must get a fresh job."""
    manager = SpeculativeCompressionManager(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    snapshot = _snapshot()
    calls = []

    class Worker:
        def __init__(self, snap):
            self.snap = snap

        def compress(self, messages, **_kwargs):
            calls.append(self.snap.source_fingerprint[:8])
            started.set()
            release.wait(2)
            return [
                {"role": "user", "content": "summary"},
                *messages[self.snap.cut_index :],
            ]

    newer = SpeculativeSnapshot(
        session_id=snapshot.session_id,
        messages=snapshot.messages,
        source_fingerprint="deadbeef" + snapshot.source_fingerprint[8:],
        boundary_fingerprint=snapshot.boundary_fingerprint,
        cut_index=snapshot.cut_index,
        compress_start=snapshot.compress_start,
        original_count=snapshot.original_count,
        request_tokens=snapshot.request_tokens,
        captured_at=snapshot.captured_at,
    )
    try:
        assert (
            manager.maybe_start("session-1", snapshot, lambda: Worker(snapshot))
            == "started"
        )
        assert started.wait(1)
        # Newer prefix arrives while the first job is still running.
        assert (
            manager.maybe_start("session-1", newer, lambda: Worker(newer))
            == "coalesced"
        )
        release.set()
        candidate = manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=2
        )
        # The rerun candidate targets the newer fingerprint, so it cannot
        # match the live transcript; nothing may be installed.
        assert candidate is None
        assert len(calls) == 2
    finally:
        release.set()
        manager.shutdown()
