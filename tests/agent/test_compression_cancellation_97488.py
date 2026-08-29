"""Regressions for compression cancellation and cooldown overflow recovery.

The total host ceiling bounds the complete lean compaction plan, including
chunk digests.  A cancelled plan must stop before starting another digest, and
its still-running worker must keep the session lease until it unwinds.  A
context overflow observed during the resulting cooldown is temporary and must
not be reported as permanent compression exhaustion.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.context_compressor as context_compressor_module
from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.context_compressor import ContextCompressor
from agent.conversation_compression import (
    CompressionCommitFence,
    compression_deferred_reason,
    run_compress_context_with_progress_timeout,
)
from agent.conversation_loop import _compression_deferred_result


def _digest_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="digest"))]
    )


def test_cancelled_lean_digest_does_not_start_the_next_request(monkeypatch):
    compressor = object.__new__(ContextCompressor)
    cancelled = threading.Event()
    compressor._compression_cancelled_check = cancelled.is_set
    monkeypatch.setattr(
        context_compressor_module, "_LEAN_DIGEST_CHUNK_CHARS", 10
    )
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        cancelled.set()
        return _digest_response()

    turns = [{"role": "user", "content": "x" * 40}]
    with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm):
        with pytest.raises(AuxiliaryExplicitCancellation):
            compressor._build_chunk_digests(turns)

    assert len(calls) == 1


def test_total_ceiling_keeps_cancelled_worker_lease_until_unwind():
    original = [{"role": "user", "content": "keep"}]
    started = threading.Event()
    release_worker = threading.Event()
    worker_done = threading.Event()
    fence = CompressionCommitFence()
    release_lease = MagicMock()
    fence.release_cancelled_compression_lock = release_lease

    def worker(_fence):
        started.set()
        try:
            while not release_worker.wait(0.005):
                # Simulate healthy sequential digest progress. The total
                # ceiling, rather than the inactivity window, must win.
                _fence.touch_progress()
            return original, "fallback"
        finally:
            worker_done.set()

    result = run_compress_context_with_progress_timeout(
        worker=worker,
        messages=original,
        system_prompt_fallback="fallback",
        idle_timeout_seconds=0.02,
        total_ceiling_seconds=0.08,
        fence=fence,
        stall_fallback=False,
    )
    assert started.wait(timeout=1)
    assert result == (original, "fallback")

    # The total-ceiling path leaves the lease for the worker's own cleanup;
    # releasing it from the host would allow an overlapping compaction.
    assert not release_lease.called
    release_worker.set()
    assert worker_done.wait(timeout=1)


def test_active_cooldown_is_a_soft_overflow_defer():
    compressor = SimpleNamespace(
        get_active_compression_failure_cooldown=lambda: {
            "remaining_seconds": 30,
        }
    )
    agent = SimpleNamespace(
        session_id="cooldown-session",
        context_compressor=compressor,
        _compression_skipped_due_to_lock=None,
        _flush_status_buffer=lambda: None,
    )

    assert compression_deferred_reason(agent) == "cooldown"
    result = _compression_deferred_result(
        agent, [{"role": "user"}], 1, reason="cooldown"
    )

    assert result["compression_deferred"] is True
    assert "compression_exhausted" not in result
    assert result["failed"] is False
    assert "cooling down" in result["final_response"]
