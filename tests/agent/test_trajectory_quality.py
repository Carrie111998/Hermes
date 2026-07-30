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
