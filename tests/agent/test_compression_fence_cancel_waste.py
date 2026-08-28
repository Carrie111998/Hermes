"""A fence-cancelled compression attempt must stop wasting work.

Observed shape (2026-08-28, session 20260828_034232_fc3e4e): a ~600K-token
session's compression attempts each ran 32-35 minutes while the host's
progress-aware wait gave up at the 600s ceiling. Three compounding leaks:

* lean-mode ``_build_chunk_digests`` runs up to 28 sequential summary-LLM
  calls AFTER the main summary call and never consulted the host-cancellation
  probe, so a fence-cancelled worker kept burning provider calls whose commit
  was guaranteed to be refused;
* the begin_commit-refusal rollback (``_restore_compressor_attempt_state``)
  ran tens of minutes late and clobbered the newer timeout cooldown the host
  had recorded meanwhile — in memory and in the durable row — so the next
  auto-compress re-fired almost immediately and the cooldown ladder never
  climbed;
* the ladder topped out at 900s, re-burning a structurally doomed multi-minute
  attempt every ~15 minutes forever.

These tests pin the fixes for all three.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent.context_compressor as cc_module
from agent.context_compressor import ContextCompressor
from agent.conversation_compression import (
    _restore_compressor_attempt_state,
)


# ---------------------------------------------------------------------------
# Chunk digests stop as soon as host cancellation wins
# ---------------------------------------------------------------------------


def _make_compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100000
    ):
        return ContextCompressor(model="main-model", quiet_mode=True)


def _digest_turns():
    # Enough serialized text for several chunks once the chunk size is
    # patched small below.
    return [
        {"role": "user", "content": f"question {i} " + "q" * 120}
        if i % 2 == 0
        else {"role": "assistant", "content": f"answer {i} " + "a" * 120}
        for i in range(10)
    ]


def _digest_response(text="segment digest body"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_chunk_digests_stop_after_host_cancellation():
    compressor = _make_compressor()
    state = {"cancelled": False}
    compressor._compression_cancelled_check = lambda: state["cancelled"]
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        # Host cancellation wins while the first digest call is in flight.
        state["cancelled"] = True
        return _digest_response()

    with patch.object(cc_module, "_LEAN_DIGEST_CHUNK_CHARS", 300), patch(
        "agent.auxiliary_client.call_llm", side_effect=_fake_call_llm
    ):
        result = compressor._build_chunk_digests(_digest_turns())

    assert len(calls) == 1, (
        "after cancellation, no further digest LLM calls may be issued"
    )
    assert "compression attempt cancelled" in result
    assert "segment digest body" in result, (
        "digests completed before cancellation are kept"
    )


def test_chunk_digests_pre_cancelled_make_no_llm_calls():
    compressor = _make_compressor()
    compressor._compression_cancelled_check = lambda: True
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _digest_response()

    with patch.object(cc_module, "_LEAN_DIGEST_CHUNK_CHARS", 300), patch(
        "agent.auxiliary_client.call_llm", side_effect=_fake_call_llm
    ):
        result = compressor._build_chunk_digests(_digest_turns())

    assert calls == []
    assert "compression attempt cancelled" in result


def test_chunk_digests_without_probe_run_every_chunk():
    """No installed probe (unfenced callers) keeps the historical behavior."""
    compressor = _make_compressor()
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _digest_response()

    with patch.object(cc_module, "_LEAN_DIGEST_CHUNK_CHARS", 300), patch(
        "agent.auxiliary_client.call_llm", side_effect=_fake_call_llm
    ):
        result = compressor._build_chunk_digests(_digest_turns())

    assert len(calls) > 1, "small chunk size must produce several chunks"
    assert "compression attempt cancelled" not in result


def test_chunk_digests_probe_errors_fail_open():
    """A broken probe must not kill the digest pass."""
    compressor = _make_compressor()

    def _boom():
        raise RuntimeError("probe exploded")

    compressor._compression_cancelled_check = _boom
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _digest_response()

    with patch.object(cc_module, "_LEAN_DIGEST_CHUNK_CHARS", 300), patch(
        "agent.auxiliary_client.call_llm", side_effect=_fake_call_llm
    ):
        result = compressor._build_chunk_digests(_digest_turns())

    assert len(calls) > 1
    assert "compression attempt cancelled" not in result


# ---------------------------------------------------------------------------
# The late begin_commit-refusal rollback must not clobber a newer cooldown
# ---------------------------------------------------------------------------


class _FakeSessionDB:
    def __init__(self):
        self.calls = []

    def restore_compression_failure_cooldown_row(self, session_id, state):
        self.calls.append(("restore", session_id, state))

    def record_compression_failure_cooldown(self, session_id, deadline, error):
        self.calls.append(("record", session_id, deadline, error))

    def clear_compression_failure_cooldown(self, session_id):
        self.calls.append(("clear", session_id))


def _live_compressor(cooldown_until, *, streak=3, db=None):
    comp = SimpleNamespace()
    comp._summary_failure_cooldown_until = cooldown_until
    comp._last_summary_error = "host compress_context timeout (no summary progress)"
    comp._cooldown_persist_failed = False
    comp._consecutive_timeout_failures = streak
    comp._previous_summary = "live-summary"
    if db is not None:
        comp._session_db = db
        comp._session_id = "sess-1"
    return comp


def _older_snapshot():
    return {
        "_summary_failure_cooldown_until": 0.0,
        "_last_summary_error": None,
        "_cooldown_persist_failed": False,
        "_consecutive_timeout_failures": 0,
        "_previous_summary": "snapshot-summary",
    }


def test_preserve_newer_cooldown_keeps_host_recorded_state():
    newer_deadline = time.monotonic() + 900.0
    db = _FakeSessionDB()
    comp = _live_compressor(newer_deadline, db=db)
    snapshot = _older_snapshot()

    _restore_compressor_attempt_state(
        comp,
        snapshot,
        durable_cooldown_authoritative=True,
        durable_cooldown_state={"cooldown_until": None},
        preserve_newer_cooldown=True,
    )

    # The host's newer cooldown survives — deadline, error, and the ladder
    # counter — while non-cooldown attempt state is still rolled back.
    assert comp._summary_failure_cooldown_until == newer_deadline
    assert comp._consecutive_timeout_failures == 3
    assert comp._last_summary_error and "host compress_context" in comp._last_summary_error
    assert comp._previous_summary == "snapshot-summary"
    # The durable cooldown row is left alone too.
    assert db.calls == []
    # The caller's snapshot dict is not mutated.
    assert "_summary_failure_cooldown_until" in snapshot


def test_legacy_restore_without_flag_still_rolls_back():
    newer_deadline = time.monotonic() + 900.0
    comp = _live_compressor(newer_deadline)
    snapshot = _older_snapshot()

    _restore_compressor_attempt_state(comp, snapshot)

    assert comp._summary_failure_cooldown_until == 0.0
    assert comp._consecutive_timeout_failures == 0
    assert comp._previous_summary == "snapshot-summary"


def test_preserve_flag_with_older_live_cooldown_restores_snapshot():
    """The flag only protects NEWER live state; when the snapshot carries the
    newer (or equal) deadline the normal rollback still applies."""
    comp = _live_compressor(0.0, streak=0)
    future_deadline = time.monotonic() + 300.0
    snapshot = dict(
        _older_snapshot(),
        _summary_failure_cooldown_until=future_deadline,
        _consecutive_timeout_failures=2,
    )

    _restore_compressor_attempt_state(
        comp, snapshot, preserve_newer_cooldown=True
    )

    assert comp._summary_failure_cooldown_until == future_deadline
    assert comp._consecutive_timeout_failures == 2


# ---------------------------------------------------------------------------
# The timeout cooldown ladder gains a half-hour top rung
# ---------------------------------------------------------------------------


def test_timeout_cooldown_ladder_climbs_to_half_hour_and_saturates():
    comp = MagicMock()
    comp._consecutive_timeout_failures = 0
    comp.record_timeout_failure = ContextCompressor.record_timeout_failure.__get__(
        comp, MagicMock
    )
    comp._record_compression_failure_cooldown = MagicMock()

    for _ in range(5):
        comp.record_timeout_failure("host compress_context timeout")

    rungs = [
        call.args[0]
        for call in comp._record_compression_failure_cooldown.call_args_list
    ]
    assert rungs == [60.0, 300.0, 900.0, 1800.0, 1800.0]
