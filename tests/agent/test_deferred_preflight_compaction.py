"""Tests for the opt-in deferred-preflight compaction policy.

Covers ``agent.turn_context._should_defer_preflight`` — the pure predicate that
decides whether an over-threshold preflight compaction should be deferred so an
inbound message is never blocked by the summarization stall. The predicate is
intentionally side-effect-free so the policy can be verified without
constructing a live agent or DB.

Corresponds to the new ``compression.defer_preflight`` /
``compression.defer_preflight_after_seconds`` config keys (issue #91019).
"""

from agent.turn_context import _should_defer_preflight


def _decide(**overrides):
    """Call the predicate with sensible defaults (would compress + active => defer)."""
    kwargs = dict(
        enabled=True,
        defer_enabled=True,
        defer_after_seconds=900,
        idle_gap_seconds=60.0,   # actively worked within the window
        would_compress=True,
    )
    kwargs.update(overrides)
    return _should_defer_preflight(**kwargs)


class TestShouldDeferPreflight:

    def test_defers_when_active_within_window(self):
        # Over threshold but worked recently -> defer (never block the message).
        assert _decide() is True

    def test_does_not_defer_when_disabled(self):
        # Feature off (default) -> historical blocking preflight behaviour.
        assert _decide(defer_enabled=False) is False

    def test_does_not_defer_when_compression_off(self):
        assert _decide(enabled=False) is False

    def test_does_not_defer_when_window_zero(self):
        # 0 compacts right after the turn: no waiting, so no deferral.
        assert _decide(defer_after_seconds=0) is False

    def test_does_not_defer_when_window_negative(self):
        assert _decide(defer_after_seconds=-1) is False

    def test_does_not_defer_when_below_threshold(self):
        # Session not over threshold -> nothing to defer anyway.
        assert _decide(would_compress=False) is False

    def test_does_not_defer_when_window_elapsed(self):
        # Idle longer than the window -> deferral ends, compaction converges.
        assert _decide(idle_gap_seconds=900.0) is False
        assert _decide(idle_gap_seconds=901.0) is False

    def test_boundary_exactly_at_window_does_not_defer(self):
        # Deferral requires idle_gap < window; exactly at the window it ends.
        assert _decide(idle_gap_seconds=900.0) is False

    def test_defers_right_before_boundary(self):
        assert _decide(idle_gap_seconds=899.9) is True
