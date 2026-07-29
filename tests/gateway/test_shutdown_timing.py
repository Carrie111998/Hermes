"""Behavior contracts for the shared supervised-shutdown timing policy."""

from gateway.shutdown_timing import (
    DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S,
    DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S,
    resolve_gateway_shutdown_timing,
)


def test_default_policy_bounds_cleanup_before_systemd_escalation():
    timing = resolve_gateway_shutdown_timing(0)

    assert (
        timing.controlled_exit_deadline_s
        == DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S
    )
    assert (
        timing.systemd_timeout_stop_sec - timing.controlled_exit_deadline_s
        >= DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S
    )


def test_positive_drain_extends_both_deadlines_from_same_policy():
    immediate = resolve_gateway_shutdown_timing(0)
    drained = resolve_gateway_shutdown_timing(180)

    assert (
        drained.controlled_exit_deadline_s
        - immediate.controlled_exit_deadline_s
        == 180
    )
    assert (
        drained.systemd_timeout_stop_sec - immediate.systemd_timeout_stop_sec
        == 180
    )


def test_fractional_drain_is_rounded_up_only_for_systemd():
    timing = resolve_gateway_shutdown_timing(0.25)

    assert timing.controlled_exit_deadline_s == 60.25
    assert timing.systemd_timeout_stop_sec == 76
    assert (
        timing.systemd_timeout_stop_sec - timing.controlled_exit_deadline_s
        >= DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S
    )


def test_invalid_values_resolve_to_safe_defaults():
    timing = resolve_gateway_shutdown_timing(
        "bad",
        post_drain_cleanup_budget_s=float("nan"),
        systemd_kill_margin_s=float("inf"),
    )

    assert timing.drain_timeout_s == 0
    assert (
        timing.post_drain_cleanup_budget_s
        == DEFAULT_SHUTDOWN_POST_DRAIN_CLEANUP_BUDGET_S
    )
    assert (
        timing.systemd_kill_margin_s
        == DEFAULT_SYSTEMD_SHUTDOWN_KILL_MARGIN_S
    )


def test_explicit_nonnegative_overrides_remain_supported():
    timing = resolve_gateway_shutdown_timing(
        -5,
        post_drain_cleanup_budget_s=10,
        systemd_kill_margin_s=2,
    )

    assert timing.drain_timeout_s == 0
    assert timing.controlled_exit_deadline_s == 10
    assert timing.systemd_timeout_stop_sec == 12
