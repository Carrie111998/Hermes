"""Anti-thrash recovery: the tripped guard must not be permanent (#14694).

When two consecutive compactions each fail to clear the threshold, the
anti-thrashing breaker blocks automatic compaction. Before this fix the block
was permanent for the life of the session: nothing ever decremented
``_ineffective_compression_count`` (or ``_fallback_compression_streak``)
while blocked, so a session whose middle region was briefly too small to
compact never auto-compacted again — it grew unbounded until the provider's
hard context limit, and only ``/new`` or ``/reset`` recovered it.

The recovery contract pinned here:

* After ``_ANTI_THRASH_RECOVERY_SECONDS`` of continuous block, the gate enters
  probation: tripped counters drop to 1 strike (persisted). The existing
  per-session compression lease serializes actual concurrent attempts until
  the next real-usage verdict either clears or re-trips the breaker.
* An ineffective probe re-trips the guard on the very next verdict, and the
  next recovery waits a FULL fresh window (no immediate re-probe loop).
* An effective probe (or any fitting real-usage reading) fully clears the
  counters through the existing ``update_from_response`` path.
* The recovery deadline is persisted when the breaker trips. Gateway turns
  create a new agent per inbound message, so a restart must preserve elapsed
  recovery time instead of restarting the wait forever.
* The protection itself is preserved: inside the window the gate stays
  blocked exactly as before.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB


def _compressor(threshold_tokens: int = 10_000) -> ContextCompressor:
    cc = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=3,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    cc.threshold_tokens = threshold_tokens
    return cc


def _trip(cc: ContextCompressor) -> None:
    """Arm the breaker exactly as two ineffective real-usage verdicts do."""
    cc._record_ineffective_compression_verdict(2)


class TestRecoveryWindow:
    def test_recovery_window_is_fifteen_minutes(self):
        assert _compressor()._ANTI_THRASH_RECOVERY_SECONDS == 900.0

    def test_effective_probe_clears_the_guard_completely(self):
        cc = _compressor()
        base = 1000.0
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(cc)
            assert cc.should_compress(cc.threshold_tokens + 1) is False
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + cc._ANTI_THRASH_RECOVERY_SECONDS + 1,
        ):
            assert cc.should_compress(cc.threshold_tokens + 1) is True
            cc._verify_compaction_cleared_threshold = True
            cc.update_from_response({"prompt_tokens": cc.threshold_tokens - 500})
        assert cc._ineffective_compression_count == 0
        assert cc._anti_thrash_recovery_deadline == 0.0

    def test_fallback_streak_breaker_recovers_too(self):
        cc = _compressor()
        base = 1000.0
        with patch("agent.context_compressor.time.time", return_value=base):
            cc.record_completed_compaction(used_fallback=True)
            cc.record_completed_compaction(used_fallback=True)
            assert cc.should_compress(cc.threshold_tokens + 1) is False
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + cc._ANTI_THRASH_RECOVERY_SECONDS + 1,
        ):
            assert cc.should_compress(cc.threshold_tokens + 1) is True
        assert cc._fallback_compression_streak == 1

    def test_ineffective_probation_rearms_a_full_durable_window(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        cc = _compressor()
        cc.bind_session_state(session_db=db, session_id="sess-1")
        base = 2000.0

        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(cc)
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + cc._ANTI_THRASH_RECOVERY_SECONDS + 1,
        ):
            assert cc.should_compress(cc.threshold_tokens + 1) is True
            cc._record_ineffective_compression_verdict(2)

        fresh = _compressor()
        fresh.bind_session_state(session_db=db, session_id="sess-1")
        assert fresh._ineffective_compression_count == 2
        assert fresh._anti_thrash_recovery_deadline == (
            base + (2 * cc._ANTI_THRASH_RECOVERY_SECONDS) + 1
        )
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + (2 * cc._ANTI_THRASH_RECOVERY_SECONDS),
        ):
            assert fresh.should_compress(fresh.threshold_tokens + 1) is False

    def test_deadline_write_failure_cannot_persist_a_permanent_trip(
        self, tmp_path, monkeypatch,
    ):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        cc = _compressor()
        cc.bind_session_state(session_db=db, session_id="sess-1")

        def fail_deadline_write(*_args, **_kwargs):
            raise RuntimeError("simulated deadline write failure")

        monkeypatch.setattr(db, "set_compression_breaker_state", fail_deadline_write)

        _trip(cc)

        assert cc._ineffective_compression_count == 2
        assert db.get_compression_ineffective_count("sess-1") == 0

        fresh = _compressor()
        fresh.bind_session_state(session_db=db, session_id="sess-1")
        assert fresh.should_compress(fresh.threshold_tokens + 1) is True

class TestRestartSemantics:
    def test_elapsed_recovery_window_survives_agent_restart(self, tmp_path):
        """A later gateway turn must probe immediately after real elapsed time."""
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        base = 5000.0
        first = _compressor()
        first.bind_session_state(session_db=db, session_id="sess-1")
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(first)
            assert first.should_compress(first.threshold_tokens + 1) is False

        resumed = _compressor()
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + resumed._ANTI_THRASH_RECOVERY_SECONDS + 1,
        ):
            resumed.bind_session_state(session_db=db, session_id="sess-1")
            assert resumed.should_compress(resumed.threshold_tokens + 1) is True
        state = db.get_compression_breaker_state("sess-1")
        assert state["ineffective_count"] == 2
        assert state["probe_until"] > 0.0
        sibling = _compressor()
        with patch(
            "agent.context_compressor.time.time",
            return_value=base + resumed._ANTI_THRASH_RECOVERY_SECONDS + 2,
        ):
            sibling.bind_session_state(session_db=db, session_id="sess-1")
            assert sibling.should_compress(sibling.threshold_tokens + 1) is False

    def test_two_fresh_agents_get_exactly_one_probe(self, tmp_path):
        db_path = tmp_path / "state.db"
        setup_db = SessionDB(db_path=db_path)
        setup_db.create_session(session_id="sess-1", source="cli")
        first = _compressor()
        first.bind_session_state(session_db=setup_db, session_id="sess-1")
        base = 10_000.0
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(first)

        barrier = Barrier(2)

        def attempt() -> bool:
            db = SessionDB(db_path=db_path)
            cc = _compressor()
            cc.bind_session_state(session_db=db, session_id="sess-1")
            barrier.wait()
            return cc.should_compress(cc.threshold_tokens + 1)

        with patch(
            "agent.context_compressor.time.time",
            return_value=base + 901.0,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: attempt(), range(2)))

        assert sorted(results) == [False, True]
        state = setup_db.get_compression_breaker_state("sess-1")
        assert state["ineffective_count"] == 2
        assert state["probe_until"] == base + 1801.0
        assert state["probe_token"]

    def test_abandoned_probe_can_be_reclaimed_after_one_full_window(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        base = 20_000.0
        first = _compressor()
        first.bind_session_state(session_db=db, session_id="sess-1")
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(first)
        with patch("agent.context_compressor.time.time", return_value=base + 901.0):
            assert first.should_compress(first.threshold_tokens + 1) is True

        replacement = _compressor()
        with patch("agent.context_compressor.time.time", return_value=base + 1802.0):
            replacement.bind_session_state(session_db=db, session_id="sess-1")
            assert replacement.should_compress(replacement.threshold_tokens + 1) is True

    def test_reclaimed_probe_fences_the_stale_owner(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        base = 40_000.0
        old_owner = _compressor()
        old_owner.bind_session_state(session_db=db, session_id="sess-1")
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(old_owner)
        with patch("agent.context_compressor.time.time", return_value=base + 901.0):
            assert old_owner.should_compress(old_owner.threshold_tokens + 1) is True
        old_token = old_owner._anti_thrash_probe_token

        new_owner = _compressor()
        with patch("agent.context_compressor.time.time", return_value=base + 1802.0):
            new_owner.bind_session_state(session_db=db, session_id="sess-1")
            assert new_owner.should_compress(new_owner.threshold_tokens + 1) is True
        assert new_owner._anti_thrash_probe_token != old_token

        with patch("agent.context_compressor.time.time", return_value=base + 1803.0):
            assert old_owner.should_compress(old_owner.threshold_tokens + 1) is False
            assert new_owner.should_compress(new_owner.threshold_tokens + 1) is True

    def test_backward_clock_jump_is_bounded_to_one_fresh_window(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        first = _compressor()
        first.bind_session_state(session_db=db, session_id="sess-1")
        with patch("agent.context_compressor.time.time", return_value=10_000.0):
            _trip(first)

        rewound = _compressor()
        with patch("agent.context_compressor.time.time", return_value=1_000.0):
            rewound.bind_session_state(session_db=db, session_id="sess-1")
            assert rewound.should_compress(rewound.threshold_tokens + 1) is False
        assert db.get_compression_breaker_state("sess-1")["recovery_at"] == 1_900.0

        with patch("agent.context_compressor.time.time", return_value=1_901.0):
            assert rewound.should_compress(rewound.threshold_tokens + 1) is True

    def test_forward_clock_jump_fails_open_to_one_bounded_probe(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        first = _compressor()
        first.bind_session_state(session_db=db, session_id="sess-1")
        with patch("agent.context_compressor.time.time", return_value=1_000.0):
            _trip(first)
        with patch("agent.context_compressor.time.time", return_value=20_000.0):
            assert first.should_compress(first.threshold_tokens + 1) is True

    def test_compression_rotation_carries_the_original_deadline(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="parent", source="cli")
        db.create_session(session_id="child", source="cli")
        cc = _compressor()
        cc.bind_session_state(session_db=db, session_id="parent")
        with patch("agent.context_compressor.time.time", return_value=7000.0):
            _trip(cc)
        parent_deadline = cc._anti_thrash_recovery_deadline

        cc.on_session_start(
            "child",
            boundary_reason="compression",
            old_session_id="parent",
            session_db=db,
        )

        assert cc._anti_thrash_recovery_deadline == parent_deadline
        assert db.get_session_model_config_value(
            "child", "_compression_anti_thrash_recovery_at",
        ) == parent_deadline

    def test_compression_rotation_carries_claim_and_ownership(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="parent", source="cli")
        db.create_session(session_id="child", source="cli")
        cc = _compressor()
        cc.bind_session_state(session_db=db, session_id="parent")
        with patch("agent.context_compressor.time.time", return_value=30_000.0):
            _trip(cc)
        with patch("agent.context_compressor.time.time", return_value=30_901.0):
            assert cc.should_compress(cc.threshold_tokens + 1) is True
            cc.on_session_start(
                "child",
                boundary_reason="compression",
                old_session_id="parent",
                session_db=db,
            )

        child_state = db.get_compression_breaker_state("child")
        assert child_state["ineffective_count"] == 2
        assert child_state["probe_until"] == 31_801.0
        assert child_state["probe_token"] == cc._anti_thrash_probe_token
        assert cc._owns_anti_thrash_probe is True
        assert cc._ineffective_compression_count == 1

        sibling = _compressor()
        with patch("agent.context_compressor.time.time", return_value=30_902.0):
            sibling.bind_session_state(session_db=db, session_id="child")
            assert sibling.should_compress(sibling.threshold_tokens + 1) is False

    def test_session_reset_disarms_the_recovery_clock_durably(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session(session_id="sess-1", source="cli")
        cc = _compressor()
        cc.bind_session_state(session_db=db, session_id="sess-1")
        base = 1000.0
        with patch("agent.context_compressor.time.time", return_value=base):
            _trip(cc)
            assert cc.should_compress(cc.threshold_tokens + 1) is False
            cc.record_completed_compaction(used_fallback=True)
            cc.record_completed_compaction(used_fallback=True)
        with patch("agent.context_compressor.time.time", return_value=base + 901.0):
            assert cc.should_compress(cc.threshold_tokens + 1) is True
        assert cc._anti_thrash_probe_until > 0.0
        assert cc._anti_thrash_probe_token
        cc.on_session_reset()
        assert cc._anti_thrash_recovery_deadline == 0.0
        assert cc._ineffective_compression_count == 0
        assert db.get_compression_ineffective_count("sess-1") == 0
        assert db.get_session_model_config_value(
            "sess-1", "_compression_anti_thrash_recovery_at",
        ) is None
        state = db.get_compression_breaker_state("sess-1")
        assert state["fallback_streak"] == 0
        assert state["probe_until"] == 0.0
        assert state["probe_token"] == ""
