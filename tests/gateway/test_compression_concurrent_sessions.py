"""Behavioral tests for concurrent compression across distinct and shared sessions.

Complements ``test_compression_concurrent_fork.py`` (which tests the
agent-level lock against a real ``SessionDB``) by focusing on gateway-level
isolation guarantees:

1. Five distinct sessions compressing in parallel must not alias each other's
   session_ids (no cross-session contamination).
2. Two agents sharing the same session_id must serialize: exactly one rotates,
   the other returns its input unchanged (the no-op / lock-loser contract).

The stub-compressor pattern mirrors ``test_compression_concurrent_fork.py``:
the compressor returns deterministic output and sleeps briefly so threads
actually overlap at the OS level, making the absence of aliasing a genuine
stress test rather than a timing accident.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB

# Warm ``run_agent`` HERE, at collection.
#
# ``_build_agent_with_db`` imports it lazily inside the test body, under the
# same ``OPENROUTER_API_KEY`` patch used below so the module sees an identical
# environment either way. That import is expensive (it pulls the agent stack
# and runs plugin discovery), and paid inside the body it lands under the
# gate's per-test ``--timeout``. Measured 2026-08-11 with a second suite
# running alongside — i.e. the ordinary state of a 24-worker gate lane — the
# 60s cap fired mid-``import run_agent`` in
# ``test_concurrent_compressions_do_not_alias_sessions``, and pytest-timeout's
# thread method kills the process, so the whole file reported as "no tests
# ran". Collection is not covered by the per-test timeout, so this is the same
# one-time cost moved to an untimed place. Same fix 671b38765 applied to
# tests/gateway/test_feishu.py.
with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
    from run_agent import AIAgent  # noqa: F401


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_agent_with_db(db: SessionDB, session_id: str):
    """Construct an AIAgent wired to *db* and pinned to *session_id*.

    Mirrors the helper in test_compression_concurrent_fork.py exactly so the
    two test modules can be read side-by-side without cognitive overhead.
    """
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    # Stub the compressor: deterministic output, brief sleep to force thread overlap.
    compressor = MagicMock()

    def _compress_with_overlap(*_a, **_kw):
        time.sleep(0.25)  # match fork test sleep so threads reliably overlap
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    # Skip the lazy auxiliary-provider feasibility probe. ``_compress_context``
    # runs it just-in-time on an agent's first compression, and it is a live
    # probe: on this box it dialed OpenRouter and Nous, took the credit-error
    # path, and ran full plugin discovery (54 plugins) — ~15s per agent of
    # network-dependent latency inside the region these tests time. Nothing
    # here needs it: the compressor is stubbed, so aux-LLM availability cannot
    # change what either test asserts. Setting the flag is exactly what a
    # completed probe does.
    agent._compression_feasibility_checked = True

    compressor.compress.side_effect = _compress_with_overlap
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    # ROTATION fallback path — pin in_place=False so these keep covering the
    # concurrent-rotation lock contract regardless of the global default
    # (flipped to True in #38763).
    agent.compression_in_place = False
    return agent


_MESSAGES = [{"role": "user", "content": f"m{i}"} for i in range(20)]

# A worker that is still running when the assertions execute is not a lock-logic
# result — it is an unfinished measurement, and reading ``results`` at that
# point silently scores it as "did not compress".
#
# This file used ``join(timeout=15)`` and then asserted straight off ``results``.
# Measured 2026-08-11 with the workers instrumented: the lock WINNER took 19.2s
# and the LOSER 21.5s, so BOTH joins timed out, both entries were still None,
# and ``test_concurrent_compressions_same_session_serialize`` failed with
# "Expected exactly one agent to compress, got 0" — pointing at the lock while
# the lock had in fact serialized them correctly (one rotated, one returned its
# input unchanged). The gate's parallel workers make crossing 15s routine.
#
# So the deadline is now a hang-catcher, not a stopwatch: generous enough that
# a correct-but-slow run always completes, and a worker that blows it fails
# with what actually happened instead of masquerading as a lock defect.
_JOIN_TIMEOUT = 180


def _assert_all_finished(threads) -> None:
    stuck = [t.name for t in threads if t.is_alive()]
    assert not stuck, (
        f"Compression worker(s) {stuck} still running after {_JOIN_TIMEOUT}s — "
        "results below would be unfinished measurements, not lock-logic outcomes."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_concurrent_compressions_do_not_alias_sessions(tmp_path: Path) -> None:
    """Five distinct sessions compressing in parallel must each produce a unique
    post-compression session_id; no two agents must end up sharing an id.

    Without per-session locking there is no cross-session aliasing anyway (each
    agent generates its own timestamp + uuid suffix), but this test makes the
    invariant explicit and would catch any regression where session_id generation
    became shared state (e.g. a module-level counter or a shared random seed).
    """
    db = SessionDB(db_path=tmp_path / "state.db")

    n = 5
    parent_ids = [f"DISTINCT_PARENT_{i:02d}" for i in range(n)]
    for sid in parent_ids:
        db.create_session(sid, source="discord")

    agents = [_build_agent_with_db(db, sid) for sid in parent_ids]
    errors: list[Exception] = []

    def run(agent):
        try:
            agent._compress_context(_MESSAGES, "sys", approx_tokens=120_000)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(a,), name=f"session-{i}") for i, a in enumerate(agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)

    _assert_all_finished(threads)
    assert not errors, f"Compression raised exceptions: {errors}"

    # Every agent must have rotated to a new, unique session_id.
    new_ids = [a.session_id for a in agents]
    assert all(sid not in parent_ids for sid in new_ids), (
        "At least one agent did not rotate its session_id during compression. "
        f"parent_ids={parent_ids}  new_ids={new_ids}"
    )
    assert len(set(new_ids)) == n, (
        f"Post-compression session_ids are not unique: {new_ids}. "
        "Two agents aliased to the same id — cross-session contamination."
    )


def test_concurrent_compressions_same_session_serialize(tmp_path: Path) -> None:
    """Two agents sharing a session_id must not both rotate it.

    The per-session compression lock (added in #34351) serializes concurrent
    compress() calls keyed on the same session_id.  Exactly one agent must
    rotate (the lock winner); the other must return its messages unchanged (the
    lock loser, which detects ``len(returned) == len(input)`` and backs off).

    This is the gateway analogue of the fork test in
    ``test_compression_concurrent_fork.py`` but scoped to the two-agent /
    same-session shape most likely to occur in practice: the main-turn agent
    and its background-review fork both hitting the compression threshold.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    shared_sid = "SHARED_SESSION_CONCURRENT"
    db.create_session(shared_sid, source="discord")

    agent_a = _build_agent_with_db(db, shared_sid)
    agent_b = _build_agent_with_db(db, shared_sid)

    # Force genuine simultaneous lock contention instead of relying on a
    # ``time.sleep`` inside the compressor stub to make the threads overlap.
    # Under CI CPU starvation that sleep is not enough: one thread could
    # acquire → compress → rotate → RELEASE the lock before the other even
    # reaches ``try_acquire``, so both would acquire on the shared id and
    # both would compress (the historical "got 2" flake). A two-party
    # barrier in front of the real acquire guarantees both threads are
    # contending for the lock at the same instant, which is exactly the
    # condition this test means to assert — with zero timing dependency.
    barrier = threading.Barrier(2, timeout=15)
    _real_acquire = db.try_acquire_compression_lock

    def _barriered_acquire(*args, **kwargs):
        # Rendezvous both callers, then let the real (atomic) acquire decide
        # the single winner. Tolerate a broken barrier so a test-side timeout
        # never masquerades as a lock-logic failure.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return _real_acquire(*args, **kwargs)

    db.try_acquire_compression_lock = _barriered_acquire

    results: dict[str, list | None] = {"a": None, "b": None}
    errors: list[Exception] = []

    def run(key, agent):
        try:
            compressed, _sp = agent._compress_context(_MESSAGES, "sys", approx_tokens=120_000)
            results[key] = compressed
        except Exception as exc:
            errors.append(exc)

    t_a = threading.Thread(target=run, args=("a", agent_a), name="main_turn")
    t_b = threading.Thread(target=run, args=("b", agent_b), name="review_fork")
    t_a.start()
    t_b.start()
    t_a.join(timeout=_JOIN_TIMEOUT)
    t_b.join(timeout=_JOIN_TIMEOUT)
    _assert_all_finished([t_a, t_b])

    # Restore the real method so the post-join lock-leak assertion below
    # (and any future call) hits the unwrapped implementation.
    db.try_acquire_compression_lock = _real_acquire

    assert not errors, f"Compression raised exceptions: {errors}"

    # Count which agents actually compressed (returned fewer messages than input)
    compressed_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) < len(_MESSAGES)
    )
    unchanged_count = sum(
        1 for msgs in results.values()
        if msgs is not None and len(msgs) == len(_MESSAGES)
    )

    assert compressed_count == 1, (
        f"Expected exactly one agent to compress, got {compressed_count}. "
        "If both compressed, the lock failed to serialize. "
        "If neither compressed, both lost the lock (check lock logic)."
    )
    assert unchanged_count == 1, (
        f"Expected exactly one agent to return messages unchanged (lock loser), "
        f"got {unchanged_count}."
    )

    # Exactly one session_id rotation must have occurred.
    rotated = sum(
        1 for a in (agent_a, agent_b) if a.session_id != shared_sid
    )
    assert rotated == 1, (
        f"Expected exactly one agent to rotate session_id, got {rotated}. "
        "Both agents rotating produces a session fork (Damien's incident shape)."
    )

    # The lock must be released so future compression on the NEW session_id works.
    assert db.get_compression_lock_holder(shared_sid) is None, (
        "Compression lock leaked: still held on the parent session_id after both "
        "threads joined. Future compression on the child session would deadlock."
    )
