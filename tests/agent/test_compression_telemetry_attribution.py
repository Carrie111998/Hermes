"""Attempt telemetry attribution must survive overlapping workers.

The compressor object is shared between overlapping compression attempts (a
fence-cancelled worker stays alive on the pool while the host launches the
next attempt), and the telemetry slots on it are last-writer-wins. Observed
2026-08-28: aborted attempts emitted payloads carrying an OVERLAPPING
attempt's ``attempt_id`` and aux fields (a qwen3-pinned attempt reporting a
gpt-5.5 aux call, repeated attempt_ids with growing durations).

Attribution is now context-local per attempt (``compress_context`` opens a
scope; each pooled worker runs under its own copied context) with the
instance slots kept only as last-attempt mirrors for legacy readers. These
tests pin the attribution through abort/success interleavings.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import (
    ContextCompressor,
    begin_compression_attempt_attribution,
    current_compression_attempt_telemetry,
)
from agent.conversation_compression import (
    CompressionCommitFence,
    _emit_compression_attempt_telemetry,
    compress_context,
)


def _make_compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000
    ):
        return ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )


def _bare_agent(compressor):
    return SimpleNamespace(
        context_compressor=compressor,
        session_id="session-attr-test",
        _compression_attempt_id="",
    )


def _payloads(caplog):
    lines = [
        record.getMessage()
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    return [
        json.loads(line.split("context compression attempt telemetry: ", 1)[1])
        for line in lines
    ]


def _emit(agent, commit_status, failure_class=None):
    _emit_compression_attempt_telemetry(
        agent,
        started_at=time.monotonic(),
        commit_status=commit_status,
        split_status="aborted" if commit_status == "aborted" else "not_applicable",
        failure_class=failure_class,
    )


def _record_aux(compressor, provider, model, duration_ms):
    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "prompt"}],
        max_tokens=None,
        duration_ms=duration_ms,
        aux_provider=provider,
        aux_model=model,
    )


# ---------------------------------------------------------------------------
# Deterministic interleaving across two attempt contexts, one compressor
# ---------------------------------------------------------------------------


def test_interleaved_attempts_keep_their_own_payloads(caplog):
    compressor = _make_compressor()
    agent = _bare_agent(compressor)
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    # A begins, then B begins (overwriting the shared instance mirrors), then
    # BOTH record aux calls, then A aborts and B commits.
    ctx_a.run(
        begin_compression_attempt_attribution,
        {"attempt_id": "attempt-a", "session_id": "s", "trigger_source": "manual"},
    )
    ctx_b.run(
        begin_compression_attempt_attribution,
        {"attempt_id": "attempt-b", "session_id": "s", "trigger_source": "auto"},
    )
    ctx_a.run(lambda: compressor._begin_compression_telemetry(current_tokens=100))
    ctx_b.run(lambda: compressor._begin_compression_telemetry(current_tokens=200))
    ctx_a.run(lambda: _record_aux(compressor, "prov-a", "model-a", 111))
    ctx_b.run(lambda: _record_aux(compressor, "prov-b", "model-b", 222))

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        ctx_a.run(
            lambda: _emit(agent, "aborted", failure_class="commit_fence_cancelled")
        )
        ctx_b.run(lambda: _emit(agent, "committed"))

    aborted, committed = _payloads(caplog)
    assert aborted["attempt_id"] == "attempt-a"
    assert aborted["trigger_source"] == "manual"
    assert aborted["aux_provider"] == "prov-a"
    assert aborted["aux_model"] == "model-a"
    assert aborted["aux_call_duration_ms"] == 111
    assert aborted["current_estimated_tokens"] == 100
    assert aborted["failure_class"] == "commit_fence_cancelled"

    assert committed["attempt_id"] == "attempt-b"
    assert committed["trigger_source"] == "auto"
    assert committed["aux_provider"] == "prov-b"
    assert committed["aux_model"] == "model-b"
    assert committed["aux_call_duration_ms"] == 222
    assert committed["current_estimated_tokens"] == 200


def test_pre_summary_abort_emits_its_own_seed_not_the_shared_slot(caplog):
    """An abort that never began summary telemetry (fence refused before the
    summary phase) must emit under its own identity, not whatever overlapping
    attempt last wrote the shared mirror — the 'chunk_count:0 with a reused
    attempt_id' log shape."""
    compressor = _make_compressor()
    compressor._last_compression_telemetry = {
        "event": "compression_attempt",
        "attempt_id": "other-overlapping-attempt",
        "aux_model": "other-model",
        "aux_provider": "other-provider",
    }
    agent = _bare_agent(compressor)
    ctx = contextvars.copy_context()
    ctx.run(
        begin_compression_attempt_attribution,
        {"attempt_id": "mine", "session_id": "s", "trigger_source": "auto"},
    )

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        ctx.run(lambda: _emit(agent, "aborted", failure_class="commit_fence_cancelled"))

    (payload,) = _payloads(caplog)
    assert payload["attempt_id"] == "mine"
    assert payload.get("aux_model") != "other-model"
    assert "other-provider" not in json.dumps(payload)


def test_new_scope_resets_the_previous_attempts_telemetry():
    """Executor threads are reused: opening a new attribution scope must drop
    the previous attempt's telemetry so a pre-summary abort in the new
    attempt cannot inherit it."""
    compressor = _make_compressor()

    def _scenario():
        begin_compression_attempt_attribution(
            {"attempt_id": "first", "session_id": "s", "trigger_source": "auto"}
        )
        compressor._begin_compression_telemetry(current_tokens=100)
        assert current_compression_attempt_telemetry() is not None
        begin_compression_attempt_attribution(
            {"attempt_id": "second", "session_id": "s", "trigger_source": "auto"}
        )
        assert current_compression_attempt_telemetry() is None

    contextvars.copy_context().run(_scenario)


def test_unscoped_legacy_callers_still_read_the_shared_mirror(caplog):
    compressor = _make_compressor()
    compressor._last_compression_telemetry = {
        "event": "compression_attempt",
        "attempt_id": "legacy-attempt",
    }
    agent = _bare_agent(compressor)
    ctx = contextvars.copy_context()
    # Explicitly no scope (clears any residue from the test thread).
    ctx.run(begin_compression_attempt_attribution, None)

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        ctx.run(lambda: _emit(agent, "committed"))

    (payload,) = _payloads(caplog)
    assert payload["attempt_id"] == "legacy-attempt"


# ---------------------------------------------------------------------------
# End-to-end: two overlapping compress_context attempts, one aborts
# ---------------------------------------------------------------------------


class _TodoStore:
    def format_for_injection(self):
        return ""


class _Agent:
    def __init__(self, compressor):
        self.context_compressor = compressor
        self.session_id = "session-attr-e2e"
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


def _long_messages():
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        msgs.append({"role": "user", "content": f"user message {idx} " + "u" * 80})
        msgs.append(
            {"role": "assistant", "content": f"assistant reply {idx} " + "a" * 80}
        )
    return msgs


def test_overlapping_compress_context_attempts_emit_distinct_attribution(caplog):
    compressor = _make_compressor()
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    a_entered = threading.Event()
    a_release = threading.Event()
    call_lock = threading.Lock()
    calls = {"n": 0}

    def fake_generate(*_args, **_kwargs):
        with call_lock:
            calls["n"] += 1
            n = calls["n"]
        if n == 1:
            a_entered.set()
            assert a_release.wait(timeout=10)
            return "SUMMARY A"
        return "SUMMARY B"

    fence_a = CompressionCommitFence()
    fence_b = CompressionCommitFence()
    result_a = {}

    def run_a():
        result_a["value"] = compress_context(
            agent,
            _long_messages(),
            "system prompt",
            approx_tokens=75_000,
            force=True,  # trigger_source=manual marks attempt A's payload
            commit_fence=fence_a,
        )

    with patch.object(compressor, "_generate_summary", side_effect=fake_generate):
        with caplog.at_level(
            logging.INFO, logger="agent.conversation_compression"
        ):
            thread_a = threading.Thread(target=run_a, name="attempt-a")
            thread_a.start()
            assert a_entered.wait(timeout=10)

            # Attempt B (auto) starts and fully commits while A is still
            # blocked in its summary phase.
            compress_context(
                agent,
                _long_messages(),
                "system prompt",
                approx_tokens=75_000,
                commit_fence=fence_b,
            )

            # A's host gives up; A's late commit must be refused.
            assert fence_a.cancel_before_commit() is True
            a_release.set()
            thread_a.join(timeout=10)
            assert not thread_a.is_alive()

    payloads = _payloads(caplog)
    assert len(payloads) == 2, payloads
    aborted = [p for p in payloads if p.get("failure_class") == "commit_fence_cancelled"]
    committed = [p for p in payloads if p.get("commit_status") == "committed"]
    assert len(aborted) == 1 and len(committed) == 1, payloads

    # The regression: pre-fix, the aborted attempt emitted the shared
    # last-writer slot — attempt B's payload — so both attempt_ids matched.
    assert aborted[0]["attempt_id"] != committed[0]["attempt_id"]
    assert aborted[0]["trigger_source"] == "manual"
    assert committed[0]["trigger_source"] == "auto"
