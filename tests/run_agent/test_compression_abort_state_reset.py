"""Regression tests for #58630: every compression abort path must reset
per-attempt in-place compaction state.

After a successful in-place compaction sets ``_last_compaction_in_place=True``
(run-level gateway signal), a later attempt that aborts or skips through ANY
early-return path in ``compress_context`` must NOT reuse that stale flag as
the flush baseline: ``conversation_history_after_compression()`` would then
treat all current messages (including unflushed new turns) as persisted, and a
restart would lose them.

The fix records a per-attempt outcome (``_last_compression_attempt_recorded``/
``_last_compression_attempt_in_place``) at the very top of
``compress_context`` — before the codex-app-server route, breaker gates, lock
acquisition, rotated-parent skips, compressor-abort, no-progress and
empty-transcript returns — so every abort path leaves the attempt outcome
``None`` and callers retain the previous flush baseline.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def _make_agent(session_db):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id="abort-state-session",
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


class _InPlaceSuccessCompressor:
    _last_compress_aborted = False
    _last_summary_error = None
    compression_count = 1
    _last_compression_made_progress = True
    _last_summary_fallback_used = False
    last_compression_rough_tokens = 0
    last_prompt_tokens = 0
    last_completion_tokens = 0
    awaiting_real_usage_after_compression = False

    def compress(self, _messages, **_kwargs):
        return [
            {"role": "user", "content": "[summary] earlier state"},
            {"role": "assistant", "content": "retained tail"},
        ]


class _BreakerBlockedCompressor(_InPlaceSuccessCompressor):
    """Trips the pre-lock automatic-compression breaker gate."""

    def _automatic_compression_blocked(self):
        return True

    def compress(self, messages, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("compress() must not be reached when blocked")


class _NoProgressCompressor(_InPlaceSuccessCompressor):
    """Returns a semantically-equal transcript (no-op attempt)."""

    _last_compression_made_progress = False

    def compress(self, messages, **_kwargs):
        return [dict(m) for m in messages]


class TestAbortPathsResetPerAttemptState:
    def _in_place_success(self, agent, messages):
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        agent.context_compressor = _InPlaceSuccessCompressor()
        compacted, _ = compress_context(
            agent, messages, "system", approx_tokens=100_000
        )
        assert agent._last_compaction_in_place is True
        assert agent._last_compression_attempt_in_place is True
        return compacted, conversation_history_after_compression(
            agent, compacted, None
        )

    def test_breaker_blocked_skip_retains_previous_baseline(self):
        """Pre-lock breaker skip after an in-place success must keep baseline."""
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original, [])
            compacted, history = self._in_place_success(agent, original)

            messages = compacted + [
                {"role": "user", "content": "new request"},
                {"role": "assistant", "content": "new answer"},
            ]
            agent.context_compressor = _BreakerBlockedCompressor()
            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )
            assert returned is messages
            # Per-attempt outcome must be reset even though this attempt
            # returned before acquiring the lock.
            assert agent._last_compression_attempt_recorded is True
            assert agent._last_compression_attempt_in_place is None
            new_history = conversation_history_after_compression(
                agent, returned, history
            )
            # Skip = previous baseline stays authoritative: not all-persisted
            # (would drop the new pair on restart), not None (would re-append
            # the compacted rows).
            assert new_history is history
            db.close()

    def test_explicit_interrupt_resets_run_level_in_place_signal(self):
        """A hard interrupt after an earlier in-place success must not leave
        ``_last_compaction_in_place=True`` behind (#79391).

        Every other abort path (lock-cancelled, fence-cancelled) resets the
        run-level signal so gateway/api_server consumers cannot mistake an
        interrupted attempt for a committed in-place boundary and rewrite /
        archive the untouched transcript — which physically deletes the
        pre-compaction rows. The explicit_interrupt path was missing the same
        reset, so a session that had compressed in place earlier the day
        (stale True) lost its pre-compaction history when a later compression
        was interrupted mid-summary by an incoming user message.
        """
        from unittest.mock import MagicMock, patch as mock_patch

        from agent import auxiliary_client as aux
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            agent._cached_system_prompt = "system"

            # Simulate an earlier successful in-place compaction: the run-level
            # signal is True before this attempt starts (the exact stale state
            # issue #79391 reports for a session compressed twice that day).
            agent._last_compaction_in_place = True

            compressor = MagicMock()
            compressor.compression_count = 1
            compressor.last_prompt_tokens = 0
            compressor.last_completion_tokens = 0
            compressor._last_summary_error = None
            compressor._last_compress_aborted = False
            compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            compressor._last_compression_made_progress = False
            compressor._last_summary_fallback_used = False
            compressor._summary_failure_cooldown_until = 0.0
            compressor._cooldown_persist_failed = False
            compressor._last_summary_dropped_count = 0
            compressor._verify_compaction_cleared_threshold = False
            compressor._ineffective_compression_count = 0
            compressor._anti_thrash_recovery_deadline = 0.0
            compressor._fallback_compression_streak = 0
            compressor._consecutive_timeout_failures = 0
            compressor._previous_summary = None
            compressor._summary_has_user_turn = False
            compressor._last_summary_auth_failure = False
            compressor._last_summary_network_failure = False
            compressor._last_aux_model_failure_model = None
            compressor._summary_model_fallen_back = False
            compressor.summary_model = None
            compressor._compression_telemetry_seed = None
            compressor._last_compression_telemetry = None
            compressor._active_compression_telemetry = None

            def _interrupted_compress(current, *_args, **_kwargs):
                agent._hard_interrupt_requested.set()
                raise aux.AuxiliaryExplicitCancellation()

            compressor.compress.side_effect = _interrupted_compress
            agent.context_compressor = compressor
            agent._compression_feasibility_checked = True

            messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
            with mock_patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "test-key"}
            ), mock_patch("agent.model_metadata.get_model_context_length", return_value=100000):
                compressed, _sp = compress_context(
                    agent, messages, "system", approx_tokens=100_000
                )

            # The interrupted attempt is a true no-op: transcript unchanged...
            assert compressed == messages
            # ...AND the run-level signal must NOT claim an in-place boundary
            # was committed. A stale True would make gateway/api_server rewrite
            # the untouched transcript as if it were already compacted.
            assert agent._last_compaction_in_place is False
            db.close()

    def test_summary_abort_resets_run_level_in_place_signal(self):
        """A summary-failure abort after an earlier in-place success must reset
        ``_last_compaction_in_place`` too (#79391)."""
        from unittest.mock import MagicMock, patch as mock_patch

        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            agent._cached_system_prompt = "system"
            agent._last_compaction_in_place = True  # stale from earlier success

            compressor = MagicMock()
            compressor.compression_count = 1
            compressor.last_prompt_tokens = 0
            compressor.last_completion_tokens = 0
            compressor._last_summary_error = "provider 429"
            compressor._last_compress_aborted = True
            compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            compressor._last_compression_made_progress = False
            compressor._last_summary_fallback_used = False
            compressor._summary_failure_cooldown_until = 0.0
            compressor._cooldown_persist_failed = False
            compressor._last_summary_dropped_count = 0
            compressor._verify_compaction_cleared_threshold = False
            compressor._ineffective_compression_count = 0
            compressor._anti_thrash_recovery_deadline = 0.0
            compressor._fallback_compression_streak = 0
            compressor._consecutive_timeout_failures = 0
            compressor._previous_summary = None
            compressor._summary_has_user_turn = False
            compressor._last_summary_auth_failure = False
            compressor._last_summary_network_failure = False
            compressor._summary_model_fallen_back = False
            compressor.summary_model = None
            compressor._compression_telemetry_seed = None
            compressor._last_compression_telemetry = None
            compressor._active_compression_telemetry = None

            def _aborted_compress(current, *_args, **_kwargs):
                return list(current)  # no-op transcript

            compressor.compress.side_effect = _aborted_compress
            agent.context_compressor = compressor
            agent._compression_feasibility_checked = True

            messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
            with mock_patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "test-key"}
            ), mock_patch("agent.model_metadata.get_model_context_length", return_value=100000):
                compressed, _sp = compress_context(
                    agent, messages, "system", approx_tokens=100_000
                )

            assert compressed == messages
            assert agent._last_compaction_in_place is False
            db.close()


