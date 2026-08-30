"""Tests for per-session compression threshold overrides (/compthreshold).

The ``session_threshold_tokens`` override on ContextCompressor lets a user
set the compression trigger for ONE session only (via the CLI slash command
/compthreshold 80) as an absolute token count, without touching the global
``compression.threshold`` config or per-model
``compression.model_thresholds`` overrides. The override wins over every
ratio-derived trigger including the small-context floor. /new|/reset
clears it.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _make(**kw) -> ContextCompressor:
    defaults: dict = dict(
        model="deepseek-v4-flash",
        threshold_percent=0.50,
        model_thresholds={"deepseek-v4-flash": 0.40},
        quiet_mode=True,
    )
    defaults.update(kw)
    return ContextCompressor(**defaults)


class TestSessionThresholdTokenOverride:
    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_base_uses_per_model_override(self, _mock):
        """No session override: per-model override applies (1M >= 512K, no floor)."""
        cc = _make()
        assert cc.threshold_percent == 0.40
        assert cc.threshold_tokens == int(1_000_000 * 0.40)

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_session_override_beats_everything(self, _mock):
        """Session token override wins over per-model, global, and floor."""
        cc = _make()
        cc.session_threshold_tokens = 350_000
        # threshold_tokens property must return the override directly
        assert cc.threshold_tokens == 350_000
        # Global/per-model state untouched
        assert cc._base_threshold_percent == 0.40
        assert cc._config_threshold_percent == 0.50

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_update_model_keeps_session_override(self, _mock):
        """A model switch must not clobber the token override."""
        cc = _make()
        cc.session_threshold_tokens = 350_000
        cc.update_model("deepseek-v4-flash", context_length=1_000_000)
        assert cc.threshold_tokens == 350_000
        assert cc.session_threshold_tokens == 350_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_context_length_setter_keeps_session_override(self, _mock):
        """Re-resolving the window must not clobber the token override."""
        cc = _make()
        cc.session_threshold_tokens = 350_000
        cc.context_length = 1_000_000  # same-value no-op guard
        assert cc.threshold_tokens == 350_000
        cc.context_length = 2_000_000  # genuinely new window
        assert cc.threshold_tokens == 350_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_reset_restores_base(self, _mock):
        """Clearing the override restores the per-model base."""
        cc = _make()
        cc.session_threshold_tokens = 350_000
        assert cc.threshold_tokens == 350_000
        cc.session_threshold_tokens = None
        cc._threshold_tokens = None  # invalidate cache
        assert cc.threshold_tokens == int(1_000_000 * 0.40)

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_on_session_reset_clears_override(self, _mock):
        """/new|/reset drops the session override."""
        cc = _make()
        cc.session_threshold_tokens = 300_000
        cc.on_session_reset()
        assert cc.session_threshold_tokens is None

    @patch("agent.context_compressor.get_model_context_length", return_value=256_000)
    def test_override_beats_small_context_floor(self, _mock):
        """Token override wins even on small-context models (<512K).

        Unlike ratio-based overrides (which the 75% floor can raise), a token
        override is absolute: 100k on a 256k model triggers at 100k, not at
        192k (75%).
        """
        cc = _make(model_thresholds={})
        _ = cc.context_length  # resolve window (256K < 512K -> floored base 0.75)
        assert cc.threshold_percent == 0.75  # base is floored
        cc.session_threshold_tokens = 100_000
        assert cc.threshold_tokens == 100_000  # override wins, NOT 192k

    @patch("agent.context_compressor.get_model_context_length", return_value=256_000)
    def test_override_clamped_to_95pct_of_window(self, _mock):
        """An override above 95% of the window is clamped down so it still fires."""
        cc = _make(model_thresholds={})
        _ = cc.context_length
        cc.session_threshold_tokens = 500_000  # > 95% of 256k = 243,200
        assert cc.threshold_tokens == int(256_000 * 0.95)

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_override_below_64k_still_resolves(self, _mock):
        """A very small override is returned as-is (floor is 1, not 64k).

        The 64k minimum is enforced by the CLI handler, not the compressor
        itself — the compressor only clamps to [1, 95% of window].
        """
        cc = _make()
        cc.session_threshold_tokens = 10_000
        assert cc.threshold_tokens == 10_000


class TestSessionOverrideAutoLowerInteraction:
    """The auto-lower path (conversation_compression.py:1804-1825) writes
    threshold_tokens, tail_token_budget, and threshold_percent directly when
    the aux model's context is smaller than the main model's threshold.

    These tests verify the session override doesn't break that correction.
    """

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_auto_lower_setter_updates_override(self, _mock):
        """The threshold_tokens setter must update session_threshold_tokens
        when the override is active, so auto-lower's correction takes effect."""
        cc = _make()
        cc.session_threshold_tokens = 350_000
        assert cc.threshold_tokens == 350_000

        # Simulate auto-lower writing through the setter
        cc.threshold_tokens = 128_000  # auto-lower to aux model context

        # The getter must now return the lowered value, not 350K
        assert cc.threshold_tokens == 128_000, (
            f"auto-lower setter was shadowed by override: "
            f"got {cc.threshold_tokens}, expected 128_000"
        )
        assert cc.session_threshold_tokens == 128_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_auto_lower_percent_sync_with_override(self, _mock):
        """After auto-lower sets threshold_percent directly (line 1823),
        the override value should still be the effective trigger."""
        cc = _make()
        cc.session_threshold_tokens = 350_000

        # Simulate full auto-lower sequence
        cc.threshold_tokens = 128_000          # line 1804
        cc.tail_token_budget = 128_000 * 0.20  # line 1815
        cc.threshold_percent = 0.128            # line 1823

        # threshold_tokens must reflect the auto-lowered value
        assert cc.threshold_tokens == 128_000
        # tail_token_budget is independently set, so it's fine
        assert cc.tail_token_budget == 25_600

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_reset_after_auto_lower_restores_percent(self, _mock):
        """/compthreshold reset must restore threshold_percent from
        _base_threshold_percent, not leave it stuck at auto-lowered value."""
        cc = _make()
        cc.session_threshold_tokens = 350_000

        # Simulate auto-lower mutating threshold_percent
        cc.threshold_tokens = 128_000
        cc.threshold_percent = 0.128

        # Reset
        cc.session_threshold_tokens = None
        cc._threshold_tokens = None
        cc._tail_token_budget = None
        _base = getattr(cc, "_base_threshold_percent", None)
        if _base is not None:
            _ctx = getattr(cc, "context_length", 0) or 0
            cc.threshold_percent = cc._effective_threshold_percent(_ctx, _base)

        # threshold_percent must be restored to base (0.40 per-model override)
        assert cc.threshold_percent == 0.40, (
            f"threshold_percent not restored: got {cc.threshold_percent}, "
            f"expected 0.40"
        )


class TestApplySessionThresholdOverride:
    """Structured API shared by the CLI slash command and the TUI gateway
    (session.compthreshold RPC / command.dispatch)."""

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_show_without_override(self, _mock):
        cc = _make()
        r = cc.apply_session_threshold_override("")
        assert r["ok"] is True
        assert r["action"] == "show"
        assert r["source"] == "global/per-model"
        assert r["override"] is None
        assert r["effective"] == 400_000  # per-model 0.40 of 1M
        assert "400,000" in r["message"]

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_set_bare_number_is_k(self, _mock):
        cc = _make()
        r = cc.apply_session_threshold_override("80")
        assert r["ok"] is True
        assert r["action"] == "set"
        assert r["override"] == 80_000
        assert r["effective"] == 80_000
        assert cc.session_threshold_tokens == 80_000
        # show now reports the override
        r2 = cc.apply_session_threshold_override("")
        assert r2["source"] == "THIS session (override)"
        assert r2["override"] == 80_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_set_explicit_k_and_m_suffixes(self, _mock):
        cc = _make()
        assert cc.apply_session_threshold_override("80k")["override"] == 80_000
        # 1.5M exceeds the 95% ceiling (950K) → clamped (see clamp test)
        assert cc.apply_session_threshold_override("1.5m")["override"] == 950_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_set_clamps_to_95_percent(self, _mock):
        cc = _make()
        r = cc.apply_session_threshold_override("1.5m")  # 1.5M > 950K ceiling
        assert r["ok"] is True
        assert r["override"] == 950_000
        assert "clamped" in r["message"]

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_set_below_floor_rejected(self, _mock):
        cc = _make()
        r = cc.apply_session_threshold_override("40")
        assert r["ok"] is False
        assert r["action"] == "error"
        assert "64,000" in r["message"]

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_set_garbage_rejected(self, _mock):
        cc = _make()
        assert cc.apply_session_threshold_override("abc")["ok"] is False

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_reset_clears_override(self, _mock):
        cc = _make()
        cc.apply_session_threshold_override("80")
        r = cc.apply_session_threshold_override("reset")
        assert r["ok"] is True
        assert r["action"] == "reset"
        assert r["override"] is None
        assert cc.session_threshold_tokens is None
        assert r["effective"] == 400_000
        assert cc.threshold_tokens == 400_000

    @patch("agent.context_compressor.get_model_context_length", return_value=1_000_000)
    def test_override_backed_by_effective_trigger(self, _mock):
        """Setting the override must immediately drive threshold_tokens."""
        cc = _make()
        cc.apply_session_threshold_override("80")
        assert cc.threshold_tokens == 80_000
        # A prompt token count above the override must fire the trigger
        assert cc.should_compress(81_000) is True
        assert cc.should_compress(79_000) is False
        cc.apply_session_threshold_override("reset")
        assert cc.should_compress(81_000) is False  # base is 400K now