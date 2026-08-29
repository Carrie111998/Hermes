"""Regressions for the #76354 review of the compression timeout architecture.

Every test here asserts the BLOCKED/hung state itself where the review demands
it — the worker is released only AFTER the assertion (helix4u called out two
prior tests that released before asserting; do not regress that).

Covers:
- F1: commit-phase overrun warning fires WHILE the commit is hung (lock-free
  ``commit_in_flight`` phase marker).
- F2: every host unwind (KeyboardInterrupt / generic exception) revokes commit
  admission before the host resumes.
- F4 (unit half): a cancelled attempt cannot clear the failure cooldown
  (fence check ordered BEFORE cooldown-clear).
- F6: bounded admission — four wedged workers refuse a fifth submission fast,
  and the refused job never runs later; a cancelled fence skips summary work.
- S3 analogue: the idle wait is charged from the last progress event, so
  silence cannot approach 2x the configured idle timeout.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time

import pytest

import agent.conversation_compression as cc
from agent.conversation_compression import (
    CompressionCommitFence,
    run_compress_context_with_progress_timeout,
)


def _drain_admission_slots():
    """Best-effort wait for pool admission slots to free between tests."""
    deadline = time.time() + 5
    while time.time() < deadline:
        with cc._compress_admission_lock:
            if cc._compress_admitted_count == 0:
                return
        time.sleep(0.02)


class TestF1CommitOverrunWhileHung:
    def test_overrun_warning_fires_while_commit_still_blocked(self):
        """The warning + on_commit_overrun fire DURING the hang, not after.

        The fake commit is event-gated and is NOT released until after the
        assertions on the callback/log have been made while the worker
        thread is still blocked inside the commit boundary.
        """
        original = [{"role": "user", "content": "a"}]
        compressed = [{"role": "assistant", "content": "late"}]
        entered = threading.Event()
        release = threading.Event()
        overrun_fired = threading.Event()
        overruns = []

        def worker(fence: CompressionCommitFence):
            assert fence.begin_commit()
            entered.set()
            try:
                # Hung commit: blocked until the TEST releases it, which
                # happens only after asserting the overrun surfaced.
                assert release.wait(timeout=10)
                return (compressed, "committed-late")
            finally:
                fence.finish_commit()

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        def on_overrun(waited, ceil):
            overruns.append((waited, ceil))
            overrun_fired.set()

        done = {}

        def run():
            done["result"] = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=original,
                system_prompt_fallback="fallback",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.05,
                on_commit_overrun=on_overrun,
            )

        comp_logger = logging.getLogger("agent.conversation_compression")
        handler = _Capture(level=logging.WARNING)
        comp_logger.addHandler(handler)
        try:
            t = threading.Thread(target=run, name="f1-hung-commit-host")
            t.start()
            try:
                assert entered.wait(timeout=2)
                # ── Assert WHILE the commit worker is still blocked ──────
                assert overrun_fired.wait(timeout=5), (
                    "on_commit_overrun must fire while the commit is hung"
                )
                assert not release.is_set()  # worker provably still blocked
                assert t.is_alive()
                deadline = time.time() + 5
                while time.time() < deadline:
                    if any(
                        r.levelno >= logging.WARNING
                        and "past the total ceiling" in r.getMessage()
                        for r in list(records)
                    ):
                        break
                    time.sleep(0.01)
                overrun_logs = [
                    r
                    for r in list(records)
                    if r.levelno >= logging.WARNING
                    and "past the total ceiling" in r.getMessage()
                ]
                assert overrun_logs, (
                    "expected the overrun WARNING while the commit was "
                    f"still blocked; got: {[r.getMessage() for r in records]}"
                )
                assert overruns and overruns[0][1] == pytest.approx(0.05)
            finally:
                release.set()
            t.join(timeout=5)
            assert not t.is_alive()
        finally:
            comp_logger.removeHandler(handler)
        assert done["result"] == (compressed, "committed-late")
        _drain_admission_slots()

    def test_commit_in_flight_marker_is_lock_free(self):
        fence = CompressionCommitFence()
        assert fence.commit_in_flight is False
        assert fence.begin_commit()
        # The fence lock is HELD here; the marker must still be readable.
        assert fence.commit_in_flight is True
        fence.finish_commit()
        assert fence.commit_in_flight is False


class _KIOnFirstResultFuture:
    """Future proxy raising on the host's first result() call."""

    def __init__(self, inner, exc, gate=None):
        self._inner = inner
        self._exc = exc
        self._raised = False
        self._gate = gate

    def result(self, timeout=None):
        if not self._raised:
            self._raised = True
            if self._gate is not None:
                # Ensure the pooled worker has genuinely STARTED before the
                # host unwinds, so the test exercises "unwind with a live
                # worker" rather than the queued-job skip path.
                assert self._gate.wait(timeout=5)
            raise self._exc
        return self._inner.result(timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _InjectingExecutor:
    def __init__(self, inner, exc, gate=None):
        self._inner = inner
        self._exc = exc
        self._gate = gate

    def submit(self, fn, *args, **kwargs):
        return _KIOnFirstResultFuture(
            self._inner.submit(fn, *args, **kwargs), self._exc, self._gate
        )


class TestF2HostUnwindRevokesAdmission:
    @pytest.mark.parametrize(
        "exc_type", [KeyboardInterrupt, RuntimeError], ids=["ki", "generic"]
    )
    def test_unwind_revokes_commit_admission_before_host_returns(
        self, monkeypatch, exc_type
    ):
        """KI/exception while waiting → detached worker can never commit.

        The worker is still blocked pre-commit when the host unwinds; the
        assertions run BEFORE the worker is released.
        """
        original = [{"role": "user", "content": "keep"}]
        started = threading.Event()
        release = threading.Event()
        fence_box = {}
        commit_admitted = {}

        def worker(fence: CompressionCommitFence):
            fence_box["fence"] = fence
            started.set()
            assert release.wait(timeout=10)
            commit_admitted["value"] = fence.begin_commit()
            if commit_admitted["value"]:
                fence.finish_commit()
            return ([{"role": "assistant", "content": "late"}], "x")

        real_executor = cc._get_compress_timeout_executor()
        monkeypatch.setattr(
            cc,
            "_get_compress_timeout_executor",
            lambda: _InjectingExecutor(real_executor, exc_type(), gate=started),
        )

        with pytest.raises(exc_type):
            run_compress_context_with_progress_timeout(
                worker=worker,
                messages=original,
                system_prompt_fallback="fallback",
                idle_timeout_seconds=5.0,
                total_ceiling_seconds=5.0,
            )

        # ── Host has unwound; worker is STILL blocked pre-commit ─────────
        assert started.wait(timeout=2)
        fence = fence_box["fence"]
        assert not release.is_set()
        assert fence.is_cancelled, (
            "host unwind must revoke commit admission while the worker "
            "is still running"
        )
        # Now release the worker and prove its commit was refused.
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and "value" not in commit_admitted:
            time.sleep(0.01)
        assert commit_admitted.get("value") is False, (
            "a worker surviving a host unwind must be denied the commit "
            "boundary"
        )
        _drain_admission_slots()

    def test_keyboard_interrupt_keeps_running_future_admission_until_done(
        self, monkeypatch
    ):
        """A host unwind cannot release a slot owned by a running future."""
        from tools.daemon_pool import DaemonThreadPoolExecutor

        _drain_admission_slots()
        previous_limit = cc._COMPRESS_EXECUTOR_MAX_WORKERS
        cc._COMPRESS_EXECUTOR_MAX_WORKERS = 1
        executor = DaemonThreadPoolExecutor(
            max_workers=1, thread_name_prefix="c1-fix3-keyboard"
        )
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def worker(_fence):
            started.set()
            assert release.wait(timeout=10)
            finished.set()
            return ([], "done")

        monkeypatch.setattr(
            cc,
            "_get_compress_timeout_executor",
            lambda: _InjectingExecutor(
                executor, KeyboardInterrupt(), gate=started
            ),
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                run_compress_context_with_progress_timeout(
                    worker=worker,
                    messages=[{"role": "user", "content": "keep"}],
                    system_prompt_fallback="fallback",
                    idle_timeout_seconds=5.0,
                    total_ceiling_seconds=5.0,
                )

            assert started.wait(timeout=2)
            assert not finished.is_set()
            with cc._compress_admission_lock:
                assert cc._compress_admitted_count == 1

            release.set()
            assert finished.wait(timeout=5)
            _drain_admission_slots()
            with cc._compress_admission_lock:
                assert cc._compress_admitted_count == 0
        finally:
            release.set()
            _drain_admission_slots()
            executor.shutdown(wait=True)
            cc._COMPRESS_EXECUTOR_MAX_WORKERS = previous_limit
            _drain_admission_slots()


class TestF4CooldownClearOrdering:
    def test_cancelled_attempt_cannot_clear_failure_cooldown(self):
        """Fence check ordered BEFORE cooldown-clear (review F4 ordering)."""
        from agent.context_compressor import ContextCompressor

        class _FakeCompressor:
            _summary_failure_cooldown_until = 12345.0
            _last_summary_error = "timeout"
            _consecutive_timeout_failures = 2
            _cooldown_persist_failed = False
            _session_db = None
            _session_id = ""
            _compression_cancelled_check = staticmethod(lambda: True)

        fake = _FakeCompressor()
        ContextCompressor._clear_compression_failure_cooldown(fake)
        assert fake._summary_failure_cooldown_until == 12345.0, (
            "a cancelled attempt must NOT undo the host's timeout cooldown"
        )
        assert fake._consecutive_timeout_failures == 2

        # Sabotage check: with the fence reporting NOT cancelled, the clear
        # must proceed (proves the guard is the only thing blocking it).
        fake2 = _FakeCompressor()
        fake2._compression_cancelled_check = staticmethod(lambda: False)
        ContextCompressor._clear_compression_failure_cooldown(fake2)
        assert fake2._summary_failure_cooldown_until == 0.0


class TestF6ExecutorSaturation:
    def test_saturated_pool_fails_fast_and_never_runs_stale_job(self):
        """4 blocked summaries + 5th submission fails fast; recovery does not
        run the refused job."""
        _drain_admission_slots()
        release = threading.Event()
        started = threading.Barrier(5, timeout=10)  # 4 workers + main

        def blocked_worker(fence: CompressionCommitFence):
            started.wait()
            assert release.wait(timeout=30)
            return ([], "done")

        hosts = []
        results = {}

        def host(i):
            results[i] = run_compress_context_with_progress_timeout(
                worker=blocked_worker,
                messages=[{"role": "user", "content": f"m{i}"}],
                system_prompt_fallback=f"fb{i}",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.1,
            )

        try:
            for i in range(4):
                t = threading.Thread(target=host, args=(i,), name=f"sat-{i}")
                t.start()
                hosts.append(t)
            started.wait()  # all 4 workers occupy the pool
            for t in hosts:
                t.join(timeout=5)  # hosts time out; workers stay wedged
                assert not t.is_alive()

            # Admission is owned by the future, not by the host timeout. The
            # four non-cooperative futures remain bounded while detached.
            with cc._compress_admission_lock:
                assert cc._compress_admitted_count == 4

            fifth_ran = threading.Event()

            def fifth_worker(fence):
                fifth_ran.set()
                return ([], "5th")

            fifth_msgs = [{"role": "user", "content": "fifth"}]
            # Round-2 #6: the fail-fast refusal must emit the standard
            # compression-attempt telemetry with failure_class=pool_saturated.
            class _TelemetryAgent:
                session_id = "SATURATED_SESSION"
                _compression_attempt_id = "sat-attempt"

                class context_compressor:  # noqa: D106 — minimal stub
                    _last_compression_telemetry = None
                    _last_summary_fallback_used = False
                    _last_aux_model_failure_model = None

            import json as _json
            import logging as _logging

            class _CaptureHandler(_logging.Handler):
                def __init__(self):
                    super().__init__()
                    self.payloads = []

                def emit(self, record):
                    msg = record.getMessage()
                    if "compression attempt telemetry" in msg:
                        self.payloads.append(
                            _json.loads(msg.split(": ", 1)[1])
                        )

            capture = _CaptureHandler()
            _prev_level = cc.logger.level
            cc.logger.addHandler(capture)
            cc.logger.setLevel(_logging.DEBUG)
            t0 = time.monotonic()
            try:
                msgs, prompt = run_compress_context_with_progress_timeout(
                    worker=fifth_worker,
                    messages=fifth_msgs,
                    system_prompt_fallback="fifth-fallback",
                    idle_timeout_seconds=5.0,
                    total_ceiling_seconds=5.0,
                    telemetry_agent=_TelemetryAgent(),
                )
            finally:
                cc.logger.removeHandler(capture)
                cc.logger.setLevel(_prev_level)
            elapsed = time.monotonic() - t0
            # The pool is still saturated while the original workers are
            # blocked, so the fifth submission must refuse immediately.
            assert elapsed < 1.0, (
                f"saturated submission must fail fast, took {elapsed:.2f}s"
            )
            assert msgs is fifth_msgs
            assert prompt == "fifth-fallback"
            assert not fifth_ran.is_set()
            saturated = [
                p for p in capture.payloads
                if p.get("failure_class") == "pool_saturated"
            ]
            assert saturated
            assert saturated[0]["commit_status"] == "aborted"
            assert saturated[0]["session_id"] == "SATURATED_SESSION"
        finally:
            release.set()

        # Worker recovery: slots free after the owner futures finish.
        _drain_admission_slots()
        assert not fifth_ran.is_set(), (
            "saturated submission must not run later after recovery"
        )
        # Recovery restores service: a NEW submission is admitted and runs.
        msgs, prompt = run_compress_context_with_progress_timeout(
            worker=lambda fence: ([{"role": "user", "content": "ok"}], "ok"),
            messages=[{"role": "user", "content": "after"}],
            system_prompt_fallback="fb",
            idle_timeout_seconds=1.0,
            total_ceiling_seconds=2.0,
        )
        assert prompt == "ok"
        _drain_admission_slots()

    def test_cancelled_fence_skips_summary_work_before_start(self):
        """A stale job whose fence was already cancelled never runs summary.

        Drives compress_context's pre-summary fence gate directly: the fence
        is cancelled BEFORE dispatch, so the expensive compress() call must
        not run and the transcript must come back unchanged.
        """
        import os
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        import tempfile

        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as td:
            db = SessionDB(db_path=Path(td) / "state.db")
            session_id = "F6_PRESTART_FENCE"
            db.create_session(session_id, source="cli")
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
            compressor = MagicMock()
            compressor.compress.return_value = [
                {"role": "user", "content": "should-not-run"}
            ]
            compressor._last_summary_error = None
            compressor._last_compress_aborted = False
            compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            agent.context_compressor = compressor
            agent._cached_system_prompt = "sys"

            fence = CompressionCommitFence()
            assert fence.cancel_before_commit() is True

            messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
            returned, _sp = agent._compress_context(
                messages, "sys", approx_tokens=120_000, commit_fence=fence
            )

            compressor.compress.assert_not_called()
            assert returned is messages
            # The cancelled attempt must not leave the durable lock held.
            assert db.get_compression_lock_holder(session_id) is None
            db.close()


class TestS3IdleChargedFromLastProgress:
    def test_silence_cannot_approach_double_idle_timeout(self):
        """Progress early in an interval must not extend silence to ~2x idle."""
        _drain_admission_slots()
        idle = 0.4
        release = threading.Event()
        progress_at = None
        timeout_observations = []

        def worker(fence: CompressionCommitFence):
            nonlocal progress_at
            time.sleep(0.05)
            fence.touch_progress()  # early progress, then total silence
            progress_at = time.monotonic()
            assert release.wait(timeout=10)
            return ([], "late")

        def on_timeout(observed_idle, waited, since_progress):
            timeout_observations.append(
                (time.monotonic(), observed_idle, waited, since_progress)
            )

        returned_at = None
        try:
            msgs, prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=[{"role": "user", "content": "a"}],
                system_prompt_fallback="fb",
                idle_timeout_seconds=idle,
                total_ceiling_seconds=5.0,
                on_timeout=on_timeout,
                stall_fallback=False,
            )
            returned_at = time.monotonic()
        finally:
            release.set()

        assert prompt == "fb"
        assert timeout_observations
        assert progress_at is not None
        callback_at, observed_idle, waited, since_progress = timeout_observations[0]
        assert observed_idle == pytest.approx(idle)
        assert since_progress >= idle
        assert since_progress < idle * 1.5
        assert callback_at - progress_at < idle * 1.5

        # Liveness only; this is not the S3 idle-decision contract.
        assert returned_at is not None
        assert returned_at - callback_at < 1.0
        _drain_admission_slots()


class TestRound2MidCommitLeaseRelease:
    """Round-2 #1: revoke must not release the durable lease mid-commit.

    Invariant: at no point can a second compressor acquire the durable lock
    while an admitted commit is still mutating; after the commit finishes
    post-revoke, the lease IS released promptly even if the worker thread is
    later parked (never runs its outer cleanup).
    """

    def _db_with_lease(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        session_id = "R2_MID_COMMIT_LEASE"
        db.create_session(session_id, source="cli")
        holder = "pid:worker:original"
        assert db.try_acquire_compression_lock(
            session_id, holder, ttl_seconds=60
        )
        return db, session_id, holder

    def test_revoke_during_in_flight_commit_defers_lease_release(
        self, tmp_path
    ):
        """Event-gated fake commit; assertions run WHILE it is blocked."""
        db, session_id, holder = self._db_with_lease(tmp_path)
        fence = CompressionCommitFence()
        fence.register_cancelled_lock_release(
            lambda: db.release_compression_lock(session_id, holder)
        )

        commit_entered = threading.Event()
        release_commit = threading.Event()
        commit_finished = threading.Event()

        def _committing_worker():
            assert fence.begin_commit()
            commit_entered.set()
            assert release_commit.wait(timeout=10)
            fence.finish_commit()
            commit_finished.set()
            # Park forever: the deferred release must NOT depend on this
            # thread's outer cleanup running.
            threading.Event().wait(30)

        worker = threading.Thread(target=_committing_worker, daemon=True)
        worker.start()
        assert commit_entered.wait(timeout=5)

        # Host revokes WHILE the commit is in flight.
        fence.revoke_commit_admission()

        # ── Assert the hung state BEFORE releasing the commit ────────────
        assert not commit_finished.is_set()
        assert db.get_compression_lock_holder(session_id) == holder, (
            "revoke released the durable lease while a commit was still "
            "mutating SessionDB"
        )
        assert not db.try_acquire_compression_lock(
            session_id, "pid:second:contender", ttl_seconds=60
        ), (
            "a second compressor acquired the durable lock DURING an "
            "admitted commit"
        )

        # ── Release the commit; deferred release must fire promptly ──────
        release_commit.set()
        assert commit_finished.wait(timeout=5)
        deadline = time.time() + 5
        while time.time() < deadline:
            if db.get_compression_lock_holder(session_id) is None:
                break
            time.sleep(0.01)
        assert db.get_compression_lock_holder(session_id) is None, (
            "lease was not released promptly after the post-revoke commit "
            "finished (worker thread is parked, so finish_commit must have "
            "performed the deferred release)"
        )
        assert db.try_acquire_compression_lock(
            session_id, "pid:second:contender", ttl_seconds=60
        )
        db.release_compression_lock(session_id, "pid:second:contender")

    def test_revoke_before_commit_releases_immediately_and_refuses_commit(
        self, tmp_path
    ):
        """No commit in flight → immediate release; begin_commit refused."""
        db, session_id, holder = self._db_with_lease(tmp_path)
        fence = CompressionCommitFence()
        fence.register_cancelled_lock_release(
            lambda: db.release_compression_lock(session_id, holder)
        )

        fence.revoke_commit_admission()

        # Release happened synchronously inside revoke — no worker involved.
        assert db.get_compression_lock_holder(session_id) is None, (
            "revoke before begin_commit must release the lease immediately"
        )
        assert fence.begin_commit() is False, (
            "begin_commit must be refused after admission was revoked"
        )
        assert db.try_acquire_compression_lock(
            session_id, "pid:second:contender", ttl_seconds=60
        )
        db.release_compression_lock(session_id, "pid:second:contender")


def test_host_timeout_wins_late_same_generation_cooldown_rollback(tmp_path):
    """Host timeout writes once and late same-generation rollback is inert."""
    from unittest.mock import patch

    from hermes_state import SessionDB
    from agent.context_compressor import ContextCompressor
    from agent.conversation_compression import (
        _claim_compressor_attempt,
        _publish_compression_terminal_outcome,
        _restore_compressor_attempt_state,
        _snapshot_compressor_attempt_state,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "R2_HOST_TIMEOUT_WINNER"
    db.create_session(session_id, source="cli")
    compressor = ContextCompressor(
        "test/model", quiet_mode=True, config_context_length=1000
    )
    compressor.bind_session_state(db, session_id)
    agent = type(
        "_Agent",
        (),
        {
            "context_compressor": compressor,
            "session_id": session_id,
            "_current_turn_id": "turn-timeout",
        },
    )()
    fence = CompressionCommitFence()
    generation = _claim_compressor_attempt(compressor)
    fence.bind_attempt(generation, "turn-timeout")
    snapshot = _snapshot_compressor_attempt_state(compressor)
    durable_before = db.get_compression_failure_cooldown_row(session_id)
    record = db.record_compression_failure_cooldown

    with patch.object(db, "record_compression_failure_cooldown", wraps=record) as recorded:
        _publish_compression_terminal_outcome(agent, fence, "host_timeout")
        assert fence.claim_host_timeout_record("turn-timeout") is True
        compressor.record_timeout_failure("host compress_context timeout (no summary progress)")
        first_state = {
            "cooldown": db.get_compression_failure_cooldown_row(session_id),
            "deadline": compressor._summary_failure_cooldown_until,
            "error": compressor._last_summary_error,
            "streak": compressor._consecutive_timeout_failures,
        }
        assert recorded.call_count == 1

        # Simulate the late same-generation worker unwind after the host record.
        _restore_compressor_attempt_state(
            compressor,
            snapshot,
            durable_cooldown_authoritative=True,
            durable_cooldown_state=durable_before,
            attempt_generation=generation,
            attempt_fence=fence,
        )

    assert db.get_compression_failure_cooldown_row(session_id) == first_state["cooldown"]
    assert compressor._summary_failure_cooldown_until == first_state["deadline"]
    assert compressor._last_summary_error == first_state["error"]
    assert compressor._consecutive_timeout_failures == first_state["streak"]
    assert fence.claim_host_timeout_record("turn-timeout") is False
    db.close()


def test_host_timeout_releases_compression_pool_slot_before_provider_returns(monkeypatch):
    """A timed-out owner frees admission while its provider callback remains detached."""
    from types import SimpleNamespace
    from agent import auxiliary_client as aux

    from tools.daemon_pool import DaemonThreadPoolExecutor

    _drain_admission_slots()
    previous_limit = cc._COMPRESS_EXECUTOR_MAX_WORKERS
    cc._COMPRESS_EXECUTOR_MAX_WORKERS = 1
    executor = DaemonThreadPoolExecutor(
        max_workers=1, thread_name_prefix="c1-fix2-fresh"
    )
    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: executor)
    provider_started = threading.Event()
    release_provider = threading.Event()
    provider_calls = []

    def _create(**kwargs):
        provider_calls.append(kwargs)
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    def _blocked_worker(_fence):
        return aux._relay_sync_completion(
            client, {"model": "compression", "messages": [], "timeout": 1}
        )

    try:
        result = run_compress_context_with_progress_timeout(
            worker=_blocked_worker,
            messages=[{"role": "user", "content": "keep"}],
            system_prompt_fallback="fallback",
            idle_timeout_seconds=0.05,
            total_ceiling_seconds=0.05,
        )
        assert provider_started.wait(timeout=1)
        assert result[0] is not None
        with cc._compress_admission_lock:
            assert cc._compress_admitted_count == 0
        admitted = threading.Event()
        fresh = run_compress_context_with_progress_timeout(
            worker=lambda _fence: (admitted.set() or ([], "new")),
            messages=[{"role": "user", "content": "new"}],
            system_prompt_fallback="fallback",
            idle_timeout_seconds=1,
            total_ceiling_seconds=1,
        )
        assert admitted.is_set()
        assert fresh == ([], "new")
        assert provider_calls
    finally:
        release_provider.set()
        cc._COMPRESS_EXECUTOR_MAX_WORKERS = previous_limit
        _drain_admission_slots()
        executor.shutdown(wait=True)


def test_noncooperative_compression_owner_keeps_admission_bounded(monkeypatch):
    """A legacy worker keeps its owner slot until its future really finishes."""
    from tools.daemon_pool import DaemonThreadPoolExecutor

    _drain_admission_slots()
    previous_limit = cc._COMPRESS_EXECUTOR_MAX_WORKERS
    cc._COMPRESS_EXECUTOR_MAX_WORKERS = 1
    executor = DaemonThreadPoolExecutor(
        max_workers=1, thread_name_prefix="c1-fix2-noncooperative"
    )
    monkeypatch.setattr(cc, "_get_compress_timeout_executor", lambda: executor)
    started = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()

    def noncooperative(_fence):
        started.set()
        assert release.wait(timeout=5)
        return ([], "late")

    try:
        result = run_compress_context_with_progress_timeout(
            worker=noncooperative,
            messages=[{"role": "user", "content": "keep"}],
            system_prompt_fallback="fallback",
            idle_timeout_seconds=0.05,
            total_ceiling_seconds=0.05,
        )
        assert result[0] is not None
        assert started.wait(timeout=1)
        with cc._compress_admission_lock:
            assert cc._compress_admitted_count == 1
        second = run_compress_context_with_progress_timeout(
            worker=lambda _fence: (second_ran.set() or ([], "second")),
            messages=[{"role": "user", "content": "second"}],
            system_prompt_fallback="fallback",
            idle_timeout_seconds=0.05,
            total_ceiling_seconds=0.05,
        )
        assert second == ([{"role": "user", "content": "second"}], "fallback")
        assert not second_ran.is_set()
        with cc._compress_admission_lock:
            assert cc._compress_admitted_count == 1
    finally:
        release.set()
        _drain_admission_slots()
        executor.shutdown(wait=True)
        cc._COMPRESS_EXECUTOR_MAX_WORKERS = previous_limit
