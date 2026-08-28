"""A cancelled commit fence must stop the in-flight summary call (#96953).

Before this seam the fence was consulted once before summary dispatch and never
again while the stream ran, and ``try_cancel_before_commit`` (the only cancel
path an async host can take) had no cancel event to signal with. A gateway
session-hygiene host that gave up at its total ceiling therefore left the
compression worker streaming a summary for minutes, holding one of the four
bounded compression-pool slots and paying for tokens, only to abort at the
commit fence with nothing committed.
"""

import json
import logging
import threading
import time
from unittest.mock import patch

import pytest

from agent.auxiliary_client import _run_protected_sync_provider_call
from agent.context_compressor import ContextCompressor
from agent.conversation_compression import (
    CompressionCommitFence,
    SummaryCancelSource,
    compress_context,
)


class _TodoStore:
    def format_for_injection(self):
        return ""


class _Agent:
    def __init__(self, compressor):
        self.context_compressor = compressor
        self.session_id = "session-fence-cancel-test"
        self.platform = "cli"
        self.model = "test/main-model"
        self.provider = "test-provider"
        self.tools = []
        self._compression_feasibility_checked = True
        self.compression_in_place = False
        self._memory_manager = None
        self._session_db = None
        self._todo_store = _TodoStore()
        self._cached_system_prompt = None

    def _emit_status(self, _message):
        pass

    def _emit_warning(self, _message):
        pass

    def _invalidate_system_prompt(self):
        self._cached_system_prompt = None

    def _build_system_prompt(self, system_message):
        return system_message

    def commit_memory_session(self, _messages):
        pass


def _messages():
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        msgs.append({"role": "user", "content": f"user message {idx}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {idx}"})
    return msgs


def _telemetry(caplog):
    records = [
        record.getMessage()
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    assert len(records) == 1, records
    return json.loads(
        records[0].split("context compression attempt telemetry: ", 1)[1]
    )


def _compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000
    ):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    return compressor


# ── SummaryCancelSource unit semantics ──────────────────────────────────────


def test_source_reports_fence_cancellation():
    fence = CompressionCommitFence()
    source = SummaryCancelSource(None, fence)

    assert source.is_set() is False
    assert fence.try_cancel_before_commit() is True
    assert source.is_set() is True


def test_source_reports_revoked_admission():
    fence = CompressionCommitFence()
    source = SummaryCancelSource(threading.Event(), fence)

    assert source.is_set() is False
    fence.revoke_commit_admission()
    assert source.is_set() is True


def test_source_reports_hard_interrupt_without_fence():
    event = threading.Event()
    source = SummaryCancelSource(event, None)

    assert source.is_set() is False
    event.set()
    assert source.is_set() is True


def test_source_set_forwards_to_the_hard_event():
    event = threading.Event()
    source = SummaryCancelSource(event, CompressionCommitFence())

    source.set()

    assert event.is_set() is True
    assert source.is_set() is True


def test_source_survives_broken_probes():
    class _Boom:
        def is_set(self):
            raise RuntimeError("probe exploded")

    class _BoomFence:
        @property
        def is_cancelled(self):
            raise RuntimeError("fence exploded")

    assert SummaryCancelSource(_Boom(), _BoomFence()).is_set() is False


# ── End-to-end: the streaming summary actually unwinds ──────────────────────


def test_fence_cancel_unwinds_a_streaming_summary(caplog):
    """The worker returns on cancellation instead of streaming to completion."""
    compressor = _compressor()
    agent = _Agent(compressor)
    fence = CompressionCommitFence()
    summary_finished = threading.Event()
    summary_entered = threading.Event()

    def _slow_streaming_summary(*_args, **_kwargs):
        # Emulate a provider call consumed through the protected seam: the
        # callback runs on a daemon thread while the compression worker polls
        # its captured cancel source.
        def _provider(_kwargs):
            summary_entered.set()
            for _ in range(600):  # 60s worth of "streaming"
                fence.touch_progress()
                time.sleep(0.1)
            summary_finished.set()
            return "SUMMARY THAT ARRIVES FAR TOO LATE"

        return _run_protected_sync_provider_call(_provider, {})

    def _cancel_when_streaming():
        assert summary_entered.wait(10.0), "summary never dispatched"
        # Exactly what gateway session hygiene does once it stops waiting.
        assert fence.try_cancel_before_commit() is True

    canceller = threading.Thread(target=_cancel_when_streaming, daemon=True)
    canceller.start()

    original = list(_messages())
    started = time.monotonic()
    with patch.object(compressor, "_generate_summary", _slow_streaming_summary):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, system_prompt = compress_context(
                agent,
                list(original),
                "system prompt",
                approx_tokens=75_000,
                force=True,
                commit_fence=fence,
            )
    elapsed = time.monotonic() - started
    canceller.join(timeout=5.0)

    # The worker unwound on cancellation rather than riding the stream out.
    assert elapsed < 20.0, f"worker did not unwind promptly ({elapsed:.1f}s)"
    assert not summary_finished.is_set()
    # Nothing was committed and the transcript is untouched.
    assert compressed == original
    assert system_prompt == "system prompt"

    payload = _telemetry(caplog)
    assert payload["commit_status"] == "aborted"
    assert payload["failure_class"] == "commit_fence_cancelled"


def test_hard_interrupt_still_reports_explicit_interrupt(caplog):
    """An operator /stop keeps its own failure class, not the fence's."""
    compressor = _compressor()
    agent = _Agent(compressor)
    agent._hard_interrupt_requested = threading.Event()
    fence = CompressionCommitFence()
    summary_entered = threading.Event()

    def _slow_streaming_summary(*_args, **_kwargs):
        def _provider(_kwargs):
            summary_entered.set()
            for _ in range(600):
                fence.touch_progress()
                time.sleep(0.1)
            return "SUMMARY THAT ARRIVES FAR TOO LATE"

        return _run_protected_sync_provider_call(_provider, {})

    def _interrupt_when_streaming():
        assert summary_entered.wait(10.0), "summary never dispatched"
        agent._hard_interrupt_requested.set()

    interrupter = threading.Thread(target=_interrupt_when_streaming, daemon=True)
    interrupter.start()

    with patch.object(compressor, "_generate_summary", _slow_streaming_summary):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
                commit_fence=fence,
            )
    interrupter.join(timeout=5.0)

    payload = _telemetry(caplog)
    assert payload["commit_status"] == "aborted"
    assert payload["failure_class"] == "explicit_interrupt"


def test_uncancelled_fence_lets_the_summary_commit(caplog):
    """The new cancel source must not abort a healthy compression."""
    compressor = _compressor()
    agent = _Agent(compressor)
    fence = CompressionCommitFence()

    with patch.object(compressor, "_generate_summary", return_value="SUMMARY"):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, _ = compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
                commit_fence=fence,
            )

    assert compressed is not None
    payload = _telemetry(caplog)
    assert payload["commit_status"] == "committed"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
