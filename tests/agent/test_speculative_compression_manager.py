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


class InstantWorker:
    """Completes immediately — the deadlock repro for the done-callback race."""

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def compress(self, messages, **_kwargs):
        return [
            {"role": "user", "content": "summary"},
            *messages[self.snapshot.cut_index :],
        ]


def test_manager_registers_callback_without_deadlock():
    """An already-completed future must not deadlock the manager lock.

    add_done_callback runs synchronously on a completed future; registering
    it while holding the non-reentrant manager lock froze every session
    sharing the default manager (verified critical). The callback must be
    registered outside the lock.
    """
    manager = SpeculativeCompressionManager(max_workers=1)
    snapshot = _snapshot()
    try:
        for _ in range(20):  # repeat to make a fast-completion race likely
            assert (
                manager.maybe_start(
                    "session-1", snapshot, lambda: InstantWorker(snapshot)
                )
                == "started"
            )
            candidate = manager.take_matching_candidate(
                "session-1", _snapshot_messages(), wait_seconds=2
            )
            assert candidate is not None
    finally:
        manager.shutdown()


def test_manager_prunes_completed_entries_on_access():
    """Completed entries with no live candidate are dropped on access."""
    manager = SpeculativeCompressionManager(
        max_workers=1, clock=lambda: time.monotonic() + 10_000
    )
    snapshot = _snapshot()
    try:
        assert (
            manager.maybe_start(
                "session-1", snapshot, lambda: InstantWorker(snapshot),
                max_age_seconds=1,
            )
            == "started"
        )
        # Let the job complete, then age it out (clock is +10_000s).
        time.sleep(0.05)
        manager.take_matching_candidate("session-1", _snapshot_messages())
        assert "session-1" not in manager._entries
    finally:
        manager.shutdown()


def test_manager_rerun_on_boundary_only_change():
    """A coalesced snapshot differing only in the boundary gets a rerun."""
    manager = SpeculativeCompressionManager(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    snapshot = _snapshot()
    calls = []

    class Worker:
        def __init__(self, snap):
            self.snap = snap

        def compress(self, messages, **_kwargs):
            calls.append(self.snap.boundary_fingerprint[:8])
            started.set()
            release.wait(2)
            return [
                {"role": "user", "content": "summary"},
                *messages[self.snap.cut_index :],
            ]

    newer = SpeculativeSnapshot(
        session_id=snapshot.session_id,
        messages=snapshot.messages,
        source_fingerprint=snapshot.source_fingerprint,  # unchanged prefix
        boundary_fingerprint="cafebabe" + snapshot.boundary_fingerprint[8:],
        compressor_fingerprint=snapshot.compressor_fingerprint,
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
        assert (
            manager.maybe_start("session-1", newer, lambda: Worker(newer))
            == "coalesced"
        )
        release.set()
        manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=2
        )
        # The prefix matched but the boundary changed: the old candidate is
        # unusable, so the rerun must run.
        assert len(calls) == 2
    finally:
        release.set()
        manager.shutdown()


def test_manager_restores_claimed_candidate():
    """A claimed candidate whose install was deferred can be requeued."""
    manager = SpeculativeCompressionManager(max_workers=1)
    snapshot = _snapshot()
    try:
        assert (
            manager.maybe_start(
                "session-1", snapshot, lambda: Worker(snapshot)
            )
            == "started"
        )
        candidate = manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=2
        )
        assert candidate is not None
        # The take consumed the candidate; a second take sees nothing.
        assert (
            manager.take_matching_candidate("session-1", _snapshot_messages())
            is None
        )
        # Deferred install requeues it.
        manager.restore_candidate("session-1", candidate)
        restored = manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=0
        )
        assert restored is candidate
    finally:
        manager.shutdown()


def test_manager_restore_does_not_clobber_running_job():
    """A running job for the same session wins over a restore."""
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
        candidate = manager.take_matching_candidate("session-1", _snapshot_messages())
        assert candidate is None  # job still running, nothing to take
        assert (
            manager.maybe_start(
                "session-1", snapshot, lambda: Worker(snapshot, started, release)
            )
            == "coalesced"
        )
        from agent.speculative_compression import SpeculativeCandidate

        stub = SpeculativeCandidate(
            session_id="session-1",
            source_fingerprint=snapshot.source_fingerprint,
            boundary_fingerprint=snapshot.boundary_fingerprint,
            cut_index=snapshot.cut_index,
            compress_start=snapshot.compress_start,
            original_count=snapshot.original_count,
            compressed_prefix=({"role": "user", "content": "stub"},),
            created_at=time.monotonic(),
        )
        manager.restore_candidate("session-1", stub)
        release.set()
        # The running job's own result wins; the restored stub must not
        # surface.
        result = manager.take_matching_candidate(
            "session-1", _snapshot_messages(), wait_seconds=2
        )
        assert result is not None and result is not stub
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
