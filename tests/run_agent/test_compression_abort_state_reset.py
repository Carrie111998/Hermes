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

import copy
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


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


class _RotationStateMutatingCompressor(_InPlaceSuccessCompressor):
    """Models the runtime state advanced by the real iterative compressor."""

    compression_count = 3
    _previous_summary = "summary before failed attempt"
    _summary_has_user_turn = False
    _last_compression_made_progress = False
    last_compression_rough_tokens = 271
    last_prompt_tokens = 314
    last_completion_tokens = 15
    _verify_compaction_cleared_threshold = False

    def __init__(self):
        self.completed_boundaries = 0
        self.session_boundary_callbacks = 0

    def compress(self, _messages, **_kwargs):
        self.compression_count = 4
        self._previous_summary = "uncommitted summary"
        self._summary_has_user_turn = True
        self._last_compression_made_progress = True
        self.last_compression_rough_tokens = 999
        return [
            {"role": "user", "content": "[summary] uncommitted state"},
            {"role": "assistant", "content": "retained tail"},
        ]

    def record_completed_compaction(self, **_kwargs):
        self.completed_boundaries += 1
        self._verify_compaction_cleared_threshold = True

    def on_session_start(self, *_args, **_kwargs):
        self.session_boundary_callbacks += 1

    def checkpoint_compression_transaction(self):
        return copy.deepcopy(self.__dict__)

    def rollback_compression_transaction(self, checkpoint):
        self.__dict__.clear()
        self.__dict__.update(copy.deepcopy(checkpoint))


class _RollbackFailingCompressor(_RotationStateMutatingCompressor):
    def rollback_compression_transaction(self, _checkpoint):
        self.rollback_calls = getattr(self, "rollback_calls", 0) + 1
        raise RuntimeError("simulated durable rollback failure")


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
            agent.context_compressor = _NoProgressCompressor()
            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )
            assert agent._last_compression_attempt_in_place is None
            new_history = conversation_history_after_compression(
                agent, returned, history
            )
            assert new_history is history
            # Run-level gateway signal is untouched by the aborted attempt.
            assert agent._last_compaction_in_place is True
            db.close()

    def test_rotation_boundary_still_clears_baseline(self):
        """A completed rotation attempt must still return None (full rewrite)."""
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original, [])
            agent.context_compressor = _InPlaceSuccessCompressor()
            compacted, _ = compress_context(
                agent, original, "system", approx_tokens=100_000
            )
            assert agent._last_compression_attempt_in_place is False
            assert conversation_history_after_compression(
                agent, compacted, list(original)
            ) is None
            db.close()

    def test_rotation_publish_failure_restores_runtime_without_boundary_latch(self):
        """A failed atomic child publication is a complete runtime no-op."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original, [])
            messages_before_attempt = copy.deepcopy(original)

            compressor = _RotationStateMutatingCompressor()
            agent.context_compressor = compressor
            agent._cached_system_prompt = "cached prompt before attempt"
            agent._cached_system_prompt_static = "cached static prefix"
            agent._static_rebuild_failed_for = "earlier prompt"
            agent._last_compaction_in_place = True
            events = []
            memory_commits = []
            agent.commit_memory_session = lambda committed: memory_commits.append(
                list(committed)
            )
            agent.event_callback = lambda event, payload: events.append(
                (event, payload)
            )

            with patch.object(
                db,
                "publish_compression_child",
                side_effect=RuntimeError("simulated atomic publication failure"),
            ):
                returned, returned_prompt = compress_context(
                    agent,
                    original,
                    "system",
                    approx_tokens=100_000,
                )

            assert returned is original
            assert returned == messages_before_attempt
            assert returned_prompt == "cached prompt before attempt"
            assert agent._cached_system_prompt == "cached prompt before attempt"
            assert agent._cached_system_prompt_static == "cached static prefix"
            assert agent._static_rebuild_failed_for == "earlier prompt"

            assert agent._last_compression_attempt_recorded is True
            assert agent._last_compression_attempt_in_place is None
            assert agent._last_compaction_in_place is True
            assert compressor.awaiting_real_usage_after_compression is False
            assert compressor.compression_count == 3
            assert compressor._previous_summary == "summary before failed attempt"
            assert compressor._summary_has_user_turn is False
            assert compressor._last_compression_made_progress is False
            assert compressor.last_compression_rough_tokens == 271
            assert compressor.last_prompt_tokens == 314
            assert compressor.last_completion_tokens == 15
            assert compressor._verify_compaction_cleared_threshold is False
            assert compressor.completed_boundaries == 0
            assert events == []
            # A failed atomic publish has no durable compaction boundary, so
            # memory providers must not receive on_session_end either.
            assert memory_commits == []
            db.close()

    def test_rotation_commits_memory_after_publish_before_session_id_switch(self):
        """Rotation ends memory under the old SID only after child publication."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            original_session_id = agent.session_id
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])
            agent.context_compressor = _InPlaceSuccessCompressor()
            order = []
            publish = db.publish_compression_child

            def publish_then_record(**kwargs):
                order.append(("publish", agent.session_id))
                return publish(**kwargs)

            def commit_then_record(_messages):
                order.append(("memory", agent.session_id))

            agent.commit_memory_session = commit_then_record
            with patch.object(db, "publish_compression_child", publish_then_record):
                compress_context(agent, messages, "system", approx_tokens=100_000)

            assert [event for event, _sid in order] == ["publish", "memory"]
            assert [sid for _event, sid in order] == [
                original_session_id,
                original_session_id,
            ]
            assert agent.session_id != original_session_id
            db.close()

    def test_in_place_commits_memory_after_archive(self):
        """In-place compaction waits for archive_and_compact before memory end."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            original_session_id = agent.session_id
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])
            agent.context_compressor = _InPlaceSuccessCompressor()
            order = []
            archive = db.archive_and_compact

            def archive_then_record(*args, **kwargs):
                order.append(("archive", agent.session_id))
                return archive(*args, **kwargs)

            def commit_then_record(_messages):
                order.append(("memory", agent.session_id))

            agent.commit_memory_session = commit_then_record
            with patch.object(db, "archive_and_compact", archive_then_record):
                compress_context(agent, messages, "system", approx_tokens=100_000)

            assert [event for event, _sid in order] == ["archive", "memory"]
            assert [sid for _event, sid in order] == [
                original_session_id,
                original_session_id,
            ]
            db.close()

    def test_in_place_archive_failure_is_complete_no_op(self):
        """A failed archive never surfaces a compaction boundary."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])
            before = copy.deepcopy(messages)
            compressor = _RotationStateMutatingCompressor()
            agent.context_compressor = compressor
            agent._cached_system_prompt = "cached prompt before attempt"
            agent._cached_system_prompt_static = "cached static prefix"
            agent._static_rebuild_failed_for = "earlier prompt"
            events = []
            dedup_resets = []
            memory_commits = []
            agent.event_callback = lambda event, payload: events.append((event, payload))
            agent.commit_memory_session = lambda _messages: memory_commits.append(True)

            with patch.object(
                db, "archive_and_compact", side_effect=RuntimeError("archive failed")
            ), patch("tools.file_tools.reset_file_dedup", dedup_resets.append):
                returned, returned_prompt = compress_context(
                    agent, messages, "system", approx_tokens=100_000
                )

            assert returned is messages
            assert returned == before
            assert returned_prompt == "cached prompt before attempt"
            assert agent._cached_system_prompt == "cached prompt before attempt"
            assert agent._cached_system_prompt_static == "cached static prefix"
            assert agent._static_rebuild_failed_for == "earlier prompt"
            assert compressor.compression_count == 3
            assert compressor._previous_summary == "summary before failed attempt"
            assert compressor.awaiting_real_usage_after_compression is False
            assert compressor._verify_compaction_cleared_threshold is False
            assert compressor.session_boundary_callbacks == 0
            assert events == []
            assert memory_commits == []
            assert dedup_resets == []
            db.close()

    def test_in_place_prompt_write_failure_rolls_back_archive_transaction(self):
        """The prompt write shares the archive transaction, so neither commits."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            session_id = agent.session_id
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])
            agent._cached_system_prompt = "old durable prompt"
            db.update_system_prompt(session_id, "old durable prompt")
            agent._memory_manager = object()  # force the rebuild path below
            agent._build_system_prompt = lambda _system: "trigger prompt failure"
            agent.context_compressor = _InPlaceSuccessCompressor()
            with db._lock:
                db._conn.execute(
                    "CREATE TRIGGER reject_compaction_prompt "
                    "BEFORE UPDATE OF system_prompt ON sessions "
                    "WHEN NEW.system_prompt = 'trigger prompt failure' "
                    "BEGIN SELECT RAISE(ABORT, 'prompt write failed'); END"
                )
                db._conn.commit()

            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )

            assert returned is messages
            assert [
                {key: value for key, value in message.items() if key != "_db_persisted"}
                for message in returned
            ] == [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            assert [
                {"role": message["role"], "content": message["content"]}
                for message in db.get_messages_as_conversation(session_id)
            ] == [
                {"role": message["role"], "content": message["content"]}
                for message in returned
            ]
            assert db.get_session(session_id)["system_prompt"] == "old durable prompt"
            assert agent._cached_system_prompt == "old durable prompt"
            db.close()

    def test_rollback_failure_is_raised_not_reported_as_no_op(self):
        """An unrecoverable compressor rollback is observable and stops the turn."""
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            compressor = _RollbackFailingCompressor()
            # A semantic no-progress exit still has to roll back the runtime
            # state advanced by compress().
            compressor.compress = lambda original, **_kwargs: original
            agent.context_compressor = compressor

            try:
                compress_context(agent, messages, "system", approx_tokens=100_000)
            except RuntimeError as exc:
                assert "rollback failed" in str(exc)
            else:  # pragma: no cover - safety contract
                raise AssertionError("rollback failure was incorrectly reported as no-op")
            assert compressor.rollback_calls == 1
            db.close()

    def test_real_compressor_durable_rollback_failure_is_raised_once(self):
        """A SessionDB restore failure is observable after runtime restoration."""
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            session_id = agent.session_id
            db.create_session(session_id, "cli")
            db.record_compression_failure_cooldown(
                session_id, 4_000_000_000.0, "preexisting timeout"
            )
            compressor = ContextCompressor(
                model="test/model",
                summary_model_override="aux/test-model",
                quiet_mode=True,
                config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)
            assert db.get_compression_failure_cooldown(session_id) is not None

            checkpoint = compressor.checkpoint_compression_transaction()
            compressor._fallback_to_main_for_compression(
                RuntimeError("auxiliary unavailable"), "failed"
            )
            compressor.compression_count += 1
            restore_calls = []

            def fail_cooldown_restore(*_args, **_kwargs):
                restore_calls.append(True)
                raise RuntimeError("simulated SessionDB cooldown restore failure")

            with patch.object(
                db,
                "record_compression_failure_cooldown",
                side_effect=fail_cooldown_restore,
            ):
                try:
                    compressor.rollback_compression_transaction(checkpoint)
                except RuntimeError as exc:
                    assert "persistence restore failed" in str(exc)
                else:  # pragma: no cover - safety contract
                    raise AssertionError("durable rollback failure was hidden")

            # Runtime rollback happened before the durable failure was exposed,
            # and the once guard prevents a second partial restore attempt.
            assert compressor.summary_model == "aux/test-model"
            assert compressor.compression_count == 0
            assert restore_calls == [True]
            db.close()

    def test_summary_abort_rollback_preserves_abort_and_durable_cooldown(self):
        """A real failed summary is an outcome, not speculative rewrite state."""
        from agent.context_compressor import ContextCompressor
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            session_id = "summary-abort-outcome"
            db.create_session(session_id, "cli")
            compressor = ContextCompressor(
                model="test/model",
                quiet_mode=True,
                config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)
            checkpoint = compressor.checkpoint_compression_transaction()

            compressor._previous_summary = "speculative summary"
            compressor._last_compress_aborted = True
            compressor._last_summary_error = "summary provider timed out"
            compressor._last_summary_network_failure = True
            compressor._last_compression_telemetry = {
                "event": "compression_attempt",
                "failure_class": "summary_network_failure",
            }
            compressor._record_compression_failure_cooldown(
                60.0, "summary provider timed out"
            )

            with (
                patch.object(
                    db,
                    "clear_compression_failure_cooldown",
                    wraps=db.clear_compression_failure_cooldown,
                ) as clear_cooldown,
                patch.object(
                    db,
                    "record_compression_failure_cooldown",
                    wraps=db.record_compression_failure_cooldown,
                ) as record_cooldown,
            ):
                compressor.rollback_compression_transaction(
                    checkpoint,
                    preserve_failure_outcome=True,
                )

            assert clear_cooldown.call_count == 0
            assert record_cooldown.call_count == 0

            assert compressor._previous_summary is None
            assert compressor._last_compress_aborted is True
            assert compressor._last_summary_error == "summary provider timed out"
            assert compressor._last_summary_network_failure is True
            assert compressor._last_compression_telemetry["failure_class"] == (
                "summary_network_failure"
            )
            active = compressor.get_active_compression_failure_cooldown(
                refresh=True
            )
            assert active is not None
            assert active["error"] == "summary provider timed out"
            assert db.get_compression_failure_cooldown(session_id) is not None
            db.close()

    @pytest.mark.parametrize("checkpoint_has_cooldown", [True, False])
    def test_real_sessiondb_cooldown_rollback_write_failure_is_not_swallowed(
        self, checkpoint_has_cooldown
    ):
        """Rollback must use SessionDB's strict path, not its legacy swallow.

        This deliberately patches the real DB write seam rather than replacing
        either cooldown method: before the strict path, both methods logged and
        returned, so ContextCompressor incorrectly reported a restored durable
        checkpoint.
        """
        from agent.context_compressor import ContextCompressor
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            session_id = "strict-cooldown-rollback"
            db.create_session(session_id, "cli")
            if checkpoint_has_cooldown:
                db.record_compression_failure_cooldown(
                    session_id, 4_000_000_000.0, "preexisting timeout"
                )

            compressor = ContextCompressor(
                model="test/model", quiet_mode=True, config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)
            checkpoint = compressor.checkpoint_compression_transaction()

            # Make the durable row differ from the checkpoint, so rollback
            # must respectively record or clear it.
            if checkpoint_has_cooldown:
                db.clear_compression_failure_cooldown(session_id)
            else:
                db.record_compression_failure_cooldown(
                    session_id, 4_000_000_000.0, "speculative timeout"
                )

            with patch.object(
                db,
                "_execute_write",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with pytest.raises(
                    RuntimeError, match="compression rollback persistence restore failed"
                ):
                    compressor.rollback_compression_transaction(checkpoint)

            # The row remains divergent only because the write really failed;
            # the important contract is that rollback exposes this and stops
            # its caller rather than claiming durable recovery.
            restored = db.get_compression_failure_cooldown(session_id)
            assert (restored is not None) is not checkpoint_has_cooldown
            db.close()

    def test_publish_abort_stops_when_real_cooldown_restore_is_not_durable(self):
        """The compression caller must surface a real strict-restore failure."""
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            session_id = agent.session_id
            db.create_session(session_id, "cli")
            db.record_compression_failure_cooldown(
                session_id, 4_000_000_000.0, "preexisting timeout"
            )
            compressor = ContextCompressor(
                model="test/model",
                summary_model_override="aux/test-model",
                quiet_mode=True,
                config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)

            def fallback_then_compact(_messages, **_kwargs):
                compressor._fallback_to_main_for_compression(
                    RuntimeError("auxiliary unavailable"), "failed"
                )
                return [
                    {"role": "user", "content": "[summary] uncommitted state"},
                    {"role": "assistant", "content": "retained tail"},
                ]

            compressor.compress = fallback_then_compact
            agent.context_compressor = compressor
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])
            original_execute_write = db._execute_write

            def fail_publish_then_poison_rollback(*_args, **_kwargs):
                db._execute_write = lambda _operation: (_ for _ in ()).throw(
                    sqlite3.OperationalError("database is locked")
                )
                raise RuntimeError("publish failed")

            try:
                with patch.object(
                    db,
                    "publish_compression_child",
                    side_effect=fail_publish_then_poison_rollback,
                ):
                    with pytest.raises(RuntimeError, match="compression rollback failed"):
                        compress_context(
                            agent, messages, "system", approx_tokens=100_000, force=True
                        )
            finally:
                db._execute_write = original_execute_write

            # The failed rollback propagates from the abort path; it is never
            # converted into the normal unchanged-message success result.
            assert compressor.summary_model == "aux/test-model"
            db.close()

    def test_durable_checkpoint_read_failure_aborts_before_compress_or_publish(self):
        """An unknown durable baseline fails closed before any mutation."""
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            session_id = agent.session_id
            db.create_session(session_id, "cli")
            compressor = ContextCompressor(
                model="test/model",
                quiet_mode=True,
                config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)
            compressor.compression_count = 7
            compressor.compress = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("compress must not run after checkpoint read failure")
            )
            agent.context_compressor = compressor
            messages = [{"role": "user", "content": "unchanged"}]

            with patch.object(
                db,
                "get_compression_failure_cooldown",
                side_effect=RuntimeError("simulated durable read failure"),
            ), patch.object(db, "publish_compression_child") as publish:
                try:
                    compress_context(
                        agent,
                        messages,
                        "system",
                        approx_tokens=100_000,
                        force=True,
                    )
                except RuntimeError as exc:
                    assert "checkpoint persistence read failed" in str(exc)
                else:  # pragma: no cover - safety contract
                    raise AssertionError("checkpoint read failure was not surfaced")

            assert messages == [{"role": "user", "content": "unchanged"}]
            assert compressor.compression_count == 7
            publish.assert_not_called()
            db.close()

    def test_rotation_rollback_restores_aux_fallback_and_durable_cooldown(self):
        """Failed publish rolls back every real-compressor fallback side effect."""
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = False
            session_id = agent.session_id
            db.record_compression_failure_cooldown(
                session_id, 4_000_000_000.0, "preexisting timeout"
            )
            compressor = ContextCompressor(
                model="test/model",
                summary_model_override="aux/test-model",
                quiet_mode=True,
                config_context_length=100_000,
            )
            compressor.bind_session_state(db, session_id)
            original_summary_model = compressor.summary_model
            original_cooldown = db.get_compression_failure_cooldown(session_id)

            def fallback_then_compact(_messages, **_kwargs):
                compressor._fallback_to_main_for_compression(
                    RuntimeError("auxiliary unavailable"), "failed"
                )
                compressor.compression_count += 1
                compressor._previous_summary = "uncommitted summary"
                compressor._last_compression_made_progress = True
                return [
                    {"role": "user", "content": "[summary] uncommitted state"},
                    {"role": "assistant", "content": "retained tail"},
                ]

            compressor.compress = fallback_then_compact
            agent.context_compressor = compressor
            messages = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])

            with patch.object(
                db,
                "publish_compression_child",
                side_effect=RuntimeError("publish failed"),
            ):
                returned, _ = compress_context(
                    agent, messages, "system", approx_tokens=100_000
                )

            assert returned is messages
            assert compressor.summary_model == original_summary_model
            assert compressor._previous_summary is None
            assert compressor.compression_count == 0
            assert db.get_compression_failure_cooldown(session_id) == original_cooldown
            db.close()
