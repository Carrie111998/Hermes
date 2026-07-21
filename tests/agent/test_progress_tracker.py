"""Unit tests for delegated-child non-convergence tracking."""

from agent.progress_tracker import ProgressTracker
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_default_config_keeps_non_convergence_guardrail_opt_in():
    assert DEFAULT_CONFIG["delegation"]["progress_tracker"] == {
        "enabled": False,
        "warn_after": 15,
        "halt_after": 25,
    }


class TestProgressTracker:
    def test_disabled_by_default(self):
        tracker = ProgressTracker()

        for _ in range(50):
            decision = tracker.finish_iteration(made_progress=False)

        assert decision.action == "none"
        assert decision.count == 0

    def test_warns_at_configured_threshold(self):
        tracker = ProgressTracker(enabled=True, warn_after=2, halt_after=4)

        assert tracker.finish_iteration(made_progress=False).action == "none"
        decision = tracker.finish_iteration(made_progress=False)

        assert decision.action == "warn"
        assert decision.count == 2
        assert "not converging" in decision.message

    def test_halts_at_configured_threshold(self):
        tracker = ProgressTracker(enabled=True, warn_after=2, halt_after=3)

        tracker.finish_iteration(made_progress=False)
        tracker.finish_iteration(made_progress=False)
        decision = tracker.finish_iteration(made_progress=False)

        assert decision.action == "halt"
        assert decision.count == 3
        assert "stopped" in decision.message.lower()

    def test_progress_resets_stalled_iteration_count(self):
        tracker = ProgressTracker(enabled=True, warn_after=2, halt_after=4)
        tracker.finish_iteration(made_progress=False)
        assert tracker.iterations_since_progress == 1

        decision = tracker.finish_iteration(made_progress=True)

        assert decision.action == "none"
        assert tracker.iterations_since_progress == 0

    def test_reset_clears_state_for_a_new_user_turn(self):
        tracker = ProgressTracker(enabled=True, warn_after=2, halt_after=4)
        tracker.finish_iteration(made_progress=False)
        tracker.finish_iteration(made_progress=False)
        assert tracker.iterations_since_progress == 2

        tracker.reset()

        assert tracker.iterations_since_progress == 0
        assert tracker.finish_iteration(made_progress=False).action == "none"

    def test_from_mapping_is_opt_in(self):
        assert ProgressTracker.from_mapping({"warn_after": 2}).enabled is False
        assert ProgressTracker.from_mapping({"enabled": True}).enabled is True

    def test_false_like_strings_do_not_enable_tracker(self):
        for value in ("false", "no", "off", "0", ""):
            assert ProgressTracker.from_mapping({"enabled": value}).enabled is False

    def test_true_like_strings_enable_tracker(self):
        for value in ("true", "yes", "on", "1"):
            assert ProgressTracker.from_mapping({"enabled": value}).enabled is True

    def test_from_mapping_normalizes_invalid_thresholds(self):
        tracker = ProgressTracker.from_mapping(
            {"enabled": True, "warn_after": "bad", "halt_after": -1}
        )

        assert tracker.warn_after == 15
        assert tracker.halt_after == 25

    def test_halt_threshold_cannot_precede_warning_threshold(self):
        tracker = ProgressTracker.from_mapping(
            {"enabled": True, "warn_after": 10, "halt_after": 5}
        )

        assert tracker.warn_after == 10
        assert tracker.halt_after == 10
