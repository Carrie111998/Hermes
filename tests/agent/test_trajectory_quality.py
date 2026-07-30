"""Pure unit tests for trajectory quality routing (reducer/policy).

No AIAgent, no LLM, no network. The controller is a pure function over
structured observations.
"""

from __future__ import annotations

from agent.trajectory_quality import (
    TrajectoryQualityConfig,
    TrajectoryQualityController,
    TrajectoryObservation,
    TrajectoryQualityDecision,
    build_observation,
)


# ---------------------------------------------------------------------------
# S0 — Public API importable + defaults
# ---------------------------------------------------------------------------


def test_config_defaults_disabled():
    cfg = TrajectoryQualityConfig()
    assert cfg.enabled is False
    assert cfg.identical_failure == 2
    assert cfg.same_tool_failure == 4
    assert cfg.failed_verification == 2
    assert cfg.stagnation_window == 8
    assert cfg.execute_model_switch is False
    assert cfg.allow_deescalate_on_progress is False


def test_disabled_controller_observe_returns_none():
    cfg = TrajectoryQualityConfig(enabled=False)
    ctrl = TrajectoryQualityController(cfg)
    obs = TrajectoryObservation(
        tool_name="terminal",
        args_hash="abc",
        result_hash=None,
        failed=True,
    )
    assert ctrl.observe(obs) is None


# ---------------------------------------------------------------------------
# S1 — Identical failure circuit breaker
# ---------------------------------------------------------------------------


def _failed_obs(tool_name="terminal", args_hash="aaa") -> TrajectoryObservation:
    return TrajectoryObservation(
        tool_name=tool_name,
        args_hash=args_hash,
        result_hash=None,
        failed=True,
    )


def test_two_identical_failures_escalate_to_recommend_stronger_model():
    cfg = TrajectoryQualityConfig(enabled=True)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs())
    decision = ctrl.observe(_failed_obs())
    assert decision is not None
    assert decision.action == "recommend_stronger_model"
    assert decision.reason_code == "two_identical_failures"
    assert decision.count == 2


def test_two_failures_different_args_do_not_trip_identical_breaker():
    cfg = TrajectoryQualityConfig(enabled=True)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs(args_hash="aaa"))
    decision = ctrl.observe(_failed_obs(args_hash="bbb"))
    assert decision is None


def test_success_after_one_failure_resets_exact_counter():
    cfg = TrajectoryQualityConfig(enabled=True)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs(args_hash="aaa"))
    # A successful file mutation clears the exact counter for this signature.
    success_obs = TrajectoryObservation(
        tool_name="terminal",
        args_hash="aaa",
        result_hash=None,
        failed=False,
        progress_kind="file_mutation_landed",
    )
    ctrl.observe(success_obs)
    # Now a single new failure should NOT trigger (needs 2 again).
    decision = ctrl.observe(_failed_obs(args_hash="aaa"))
    assert decision is None


def test_reset_for_turn_clears_all_counters():
    cfg = TrajectoryQualityConfig(enabled=True)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs())
    ctrl.reset_for_turn()
    decision = ctrl.observe(_failed_obs())
    assert decision is None


# ---------------------------------------------------------------------------
# S2 — Policy ladder + hysteresis
# ---------------------------------------------------------------------------


def test_same_tool_failure_streak_escalates_at_threshold():
    """same_tool_failure=4 default: 4 failures of the same tool (any args)."""
    cfg = TrajectoryQualityConfig(enabled=True)
    ctrl = TrajectoryQualityController(cfg)
    # Three different args hashes — identical breaker never fires.
    for i, h in enumerate(["a1", "a2", "a3"]):
        ctrl.observe(_failed_obs(args_hash=h))
    # Fourth failure of same tool triggers same_tool_failure_streak.
    decision = ctrl.observe(_failed_obs(args_hash="a4"))
    assert decision is not None
    assert decision.action == "recommend_stronger_model"
    assert decision.reason_code == "same_tool_failure_streak"


def test_failed_verification_streak_escalates():
    cfg = TrajectoryQualityConfig(enabled=True, failed_verification=2)
    ctrl = TrajectoryQualityController(cfg)
    obs1 = TrajectoryObservation(
        tool_name="terminal",
        args_hash="v1",
        result_hash=None,
        failed=True,
        progress_kind="verification_failed",
    )
    obs2 = TrajectoryObservation(
        tool_name="terminal",
        args_hash="v2",
        result_hash=None,
        failed=True,
        progress_kind="verification_failed",
    )
    ctrl.observe(obs1)
    decision = ctrl.observe(obs2)
    assert decision is not None
    assert decision.reason_code == "failed_verification_streak"


def test_stagnation_escalates_to_clean_restart():
    """stagnation_window with no progress after prior failure -> level 2."""
    cfg = TrajectoryQualityConfig(enabled=True, stagnation_window=4)
    ctrl = TrajectoryQualityController(cfg)
    # Seed a failure so stagnation has "seen failure" context.
    ctrl.observe(_failed_obs(args_hash="seed"))
    # Now fill with non-progressing non-failed observations.
    for i in range(4):
        ctrl.observe(
            TrajectoryObservation(
                tool_name="read_file",
                args_hash=f"r{i}",
                result_hash=None,
                failed=False,
                progress_kind="none",
            )
        )
    # Stagnation should escalate to at least recommend_clean_restart.
    assert ctrl.level in ("recommend_clean_restart", "stop")


def test_compounding_new_reason_pushes_to_level_2():
    """Already at level 1, a new distinct reason escalates to clean_restart."""
    cfg = TrajectoryQualityConfig(
        enabled=True, identical_failure=2, failed_verification=2
    )
    ctrl = TrajectoryQualityController(cfg)
    # Get to level 1 via two identical failures.
    ctrl.observe(_failed_obs(args_hash="x1"))
    d1 = ctrl.observe(_failed_obs(args_hash="x1"))
    assert d1 is not None and d1.action == "recommend_stronger_model"
    # New reason: verification failures with different args.
    v1 = TrajectoryObservation(
        tool_name="terminal", args_hash="v1", result_hash=None,
        failed=True, progress_kind="verification_failed",
    )
    v2 = TrajectoryObservation(
        tool_name="terminal", args_hash="v2", result_hash=None,
        failed=True, progress_kind="verification_failed",
    )
    ctrl.observe(v1)
    d2 = ctrl.observe(v2)
    assert d2 is not None
    assert d2.action == "recommend_clean_restart"


def test_level_2_plus_new_trigger_pushes_to_stop():
    """Any trigger while at level 2 escalates to stop."""
    cfg = TrajectoryQualityConfig(enabled=True, identical_failure=2)
    ctrl = TrajectoryQualityController(cfg)
    # Escalate to level 2 via compounding.
    ctrl.observe(_failed_obs(args_hash="x1"))
    ctrl.observe(_failed_obs(args_hash="x1"))  # level 1
    # Different tool, different args -> new reason at level 1 -> level 2.
    for i in range(4):
        ctrl.observe(_failed_obs(tool_name="patch", args_hash=f"p{i}"))
    assert ctrl.level == "recommend_clean_restart"
    # Another trigger should push to stop.
    ctrl.observe(_failed_obs(tool_name="write_file", args_hash="w1"))
    ctrl.observe(_failed_obs(tool_name="write_file", args_hash="w1"))
    assert ctrl.level == "stop"


def test_duplicate_decision_suppressed_within_turn():
    """Same (action, reason, tool, args_hash) emitted only once per turn."""
    cfg = TrajectoryQualityConfig(enabled=True, identical_failure=2)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs(args_hash="dup"))
    d1 = ctrl.observe(_failed_obs(args_hash="dup"))
    assert d1 is not None
    # Third identical failure — same key, should be suppressed.
    d2 = ctrl.observe(_failed_obs(args_hash="dup"))
    assert d2 is None


def test_progress_does_not_lower_level_by_default():
    """allow_deescalate_on_progress=False: progress never lowers level."""
    cfg = TrajectoryQualityConfig(enabled=True, identical_failure=2)
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs(args_hash="p1"))
    ctrl.observe(_failed_obs(args_hash="p1"))
    assert ctrl.level == "recommend_stronger_model"
    # File mutation success — should NOT lower the level.
    ctrl.observe(
        TrajectoryObservation(
            tool_name="write_file", args_hash="p1", result_hash=None,
            failed=False, progress_kind="file_mutation_landed",
        )
    )
    assert ctrl.level == "recommend_stronger_model"


def test_deescalate_on_progress_lowers_one_step():
    """allow_deescalate_on_progress=True + hysteresis_progress_needed=2."""
    cfg = TrajectoryQualityConfig(
        enabled=True,
        identical_failure=2,
        allow_deescalate_on_progress=True,
        hysteresis_progress_needed=2,
    )
    ctrl = TrajectoryQualityController(cfg)
    ctrl.observe(_failed_obs(args_hash="d1"))
    ctrl.observe(_failed_obs(args_hash="d1"))
    assert ctrl.level == "recommend_stronger_model"
    # Need 2 progress events to de-escalate one step.
    ctrl.observe(
        TrajectoryObservation(
            tool_name="write_file", args_hash="ok1", result_hash=None,
            failed=False, progress_kind="file_mutation_landed",
        )
    )
    assert ctrl.level == "recommend_stronger_model"  # not yet
    ctrl.observe(
        TrajectoryObservation(
            tool_name="write_file", args_hash="ok2", result_hash=None,
            failed=False, progress_kind="file_mutation_landed",
        )
    )
    assert ctrl.level == "continue"


# ---------------------------------------------------------------------------
# S3 — Event builder helpers
# ---------------------------------------------------------------------------


def test_build_observation_args_hash_stable_under_reordering():
    """The args_hash must be the same regardless of dict key order."""
    obs1 = build_observation(
        tool_name="terminal",
        args={"command": "ls", "workdir": "/tmp"},
        result='{"exit_code": 0}',
        failed=False,
    )
    obs2 = build_observation(
        tool_name="terminal",
        args={"workdir": "/tmp", "command": "ls"},
        result='{"exit_code": 0}',
        failed=False,
    )
    assert obs1.args_hash == obs2.args_hash


def test_build_observation_secret_not_in_observation_fields():
    """A secret in args must not appear in the observation as plaintext."""
    secret = "sk-super-secret-key-1234567890"
    obs = build_observation(
        tool_name="terminal",
        args={"command": f"echo {secret}"},
        result='{"exit_code": 0}',
        failed=False,
    )
    # The observation holds only a hash — the secret must not be present
    # in any field or in the dataclass repr.
    import dataclasses

    dump = dataclasses.asdict(obs)
    joined = " ".join(str(v) for v in dump.values())
    assert secret not in joined
    assert secret not in repr(obs)


def test_build_observation_file_mutation_lands_progress():
    obs = build_observation(
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "hi"},
        result='{"bytes_written": 2}',
        failed=False,
    )
    assert obs.progress_kind == "file_mutation_landed"


def test_build_observation_failed_result():
    obs = build_observation(
        tool_name="terminal",
        args={"command": "false"},
        result='{"exit_code": 1, "output": "error"}',
        failed=True,
    )
    assert obs.failed is True
    assert obs.progress_kind == "none"
