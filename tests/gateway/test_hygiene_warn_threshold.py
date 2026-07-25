"""The post-compression "still large" warning must be reachable.

Session hygiene compresses when the transcript passes
``compression.threshold`` (default 0.5) of the model's context window, then
warns if the result is *still* large. That warning was pinned at 0.95 of the
window, which for any threshold below 0.95 is unreachable arithmetic: the
post-compression size would have to be nearly twice the size that triggered
compression in the first place.

On a 500K-window model compressing at 0.5, compression fires at ~250K and
the warning needs ~475K, so it never fires - the operator gets no signal when
compression achieves nothing and the session immediately re-compresses.
"""

import pytest

from gateway.run import hygiene_warn_token_threshold


class TestHygieneWarnTokenThreshold:
    def test_never_above_the_compression_trigger(self):
        """Compression left the session at or above the point that triggered
        it: it will re-fire next turn. That is the treadmill, and it is the
        signal worth logging."""
        window = 500_000
        compress_at = 250_000  # threshold 0.5

        warn_at = hygiene_warn_token_threshold(window, compress_at)

        assert warn_at <= compress_at

    @pytest.mark.parametrize(
        ("window", "threshold_pct"),
        [(500_000, 0.5), (1_000_000, 0.5), (272_000, 0.85), (128_000, 0.6)],
    )
    def test_reachable_for_realistic_windows(self, window, threshold_pct):
        """A warning that cannot fire is not a warning. For every realistic
        window/threshold pair, a session that compression failed to shrink
        must cross it."""
        compress_at = int(window * threshold_pct)

        warn_at = hygiene_warn_token_threshold(window, compress_at)

        # A compression that reclaimed nothing lands right back at the trigger.
        assert compress_at >= warn_at, (
            f"window={window} threshold={threshold_pct}: a no-op compression "
            f"({compress_at:,} tokens) would not warn (needs {warn_at:,})"
        )

    def test_effective_compression_does_not_warn(self):
        """The common case stays quiet: compression did its job."""
        warn_at = hygiene_warn_token_threshold(500_000, 250_000)

        assert 140_000 < warn_at, "a healthy post-compression size must not warn"

    def test_degenerate_inputs_do_not_crash(self):
        for window, compress_at in ((0, 0), (-1, 10), (100, 0)):
            assert hygiene_warn_token_threshold(window, compress_at) >= 0
