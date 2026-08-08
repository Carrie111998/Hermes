"""Threshold coercion math mixin for ContextCompressor (LB7).

Static/class methods for max-tokens coercion, threshold caps, effective
threshold percent, and threshold token computation. Composed into
ContextCompressor as a mixin base (MRO-first, SessionTitleMixin precedent).

Part of #78645 + #78647.
"""
from __future__ import annotations

from typing import Any

from agent.context_compressor_budget import (
    _SMALL_CTX_THRESHOLD_PERCENT,
    _SMALL_CTX_WINDOW_LIMIT,
)
from agent.model_metadata import MINIMUM_CONTEXT_LENGTH


class ContextCompressorThresholdMixin:
    """Threshold and max-tokens coercion helpers (extracted from the godfile)."""

    _MIN_CTX_TRIGGER_RATIO = 0.85

    # Anti-thrash recovery window (#14694): once the ineffective/fallback
    # breaker trips, automatic compaction stays blocked for this long, then
    # ONE probe attempt is allowed (counters drop to 1 strike, so another
    # ineffective pass re-trips immediately). Long enough that a genuinely
    # incompressible session isn't compacting in a loop; short enough that a
    # session which has since grown real compressible material recovers well
    # before it rides into the provider's hard context limit.
    _ANTI_THRASH_RECOVERY_SECONDS = 300.0

    @staticmethod
    def _coerce_max_tokens(value: Any) -> int | None:
        """Normalize a max_tokens value to a positive int or None.

        Only a positive integer is a real output reservation. None (provider
        default), non-numeric values, or <= 0 all mean "no reservation" — this
        keeps the threshold arithmetic safe from non-int inputs (e.g. a test
        MagicMock reaching ContextCompressor via a mocked parent agent).
        """
        if value is None:
            return None
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return None
        return ivalue if ivalue > 0 else None

    @staticmethod
    def _coerce_threshold_tokens_cap(value: Any) -> int | None:
        """Normalize a threshold_tokens cap to a positive int or None.

        None means "no absolute cap — use the ratio-based threshold only".
        Non-numeric or non-positive values are treated as None so a bad
        config value never silently caps the threshold at zero.
        """
        if value is None:
            return None
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return None
        return ivalue if ivalue > 0 else None

    def _apply_threshold_tokens_cap(self) -> None:
        """Apply the absolute token cap if configured.

        After ``threshold_tokens`` is (re)computed from the ratio-based
        percent, clamp it to the cap so compression never fires later
        than the user's preferred absolute token count. The cap itself
        is clamped to the current context length so a cap larger than
        the model's window is a no-op (the ratio-based threshold wins).
        """
        if self.threshold_tokens_cap is not None and self.threshold_tokens_cap > 0:
            _effective_cap = min(self.threshold_tokens_cap, self.context_length)
            if _effective_cap < self.threshold_tokens:
                self.threshold_tokens = _effective_cap

    @staticmethod
    def _effective_threshold_percent(
        context_length: int, threshold_percent: float,
    ) -> float:
        """Apply the small-context threshold floor (raise-only).

        Models under ``_SMALL_CTX_WINDOW_LIMIT`` (512K) trigger at no less
        than ``_SMALL_CTX_THRESHOLD_PERCENT`` (75%) of the window.  An
        explicitly higher threshold (user config or per-model autoraise,
        e.g. Codex gpt-5.5's 85%) always wins; only lower values are raised.
        Large-context models keep the configured value — at 512K+ the default
        50% trigger already leaves ample post-compaction headroom.
        """
        if context_length and context_length < _SMALL_CTX_WINDOW_LIMIT:
            return max(threshold_percent, _SMALL_CTX_THRESHOLD_PERCENT)
        return threshold_percent

    @staticmethod
    def _compute_threshold_tokens(
        context_length: int, threshold_percent: float, max_tokens: int | None = None,
    ) -> int:
        """Compute the compaction trigger threshold in tokens.

        The base value is ``effective_input_budget * threshold_percent``, floored
        at ``MINIMUM_CONTEXT_LENGTH`` so large-context models don't compress
        prematurely at 50%. BUT that floor degenerates at small windows: for a
        model whose ``context_length`` is at/below the minimum (e.g. a 64K
        local model), ``max(0.5*64000, 64000) == 64000`` makes the threshold
        equal the ENTIRE window — auto-compression can never fire because the
        provider rejects the request before usage reaches 100% (#14690).

        When the floor would meet or exceed the context window, trigger at
        ``_MIN_CTX_TRIGGER_RATIO`` (85%) of the window — high enough that a
        small model uses most of its context before compacting, but below
        100% so compaction fires before the provider rejects the request.

        The provider reserves ``max_tokens`` of output space out of the same
        window, so the usable INPUT budget is ``context_length - max_tokens``.
        With a large ``max_tokens`` (e.g. 65536 on a custom provider) the input
        budget is materially smaller than the raw window, and a threshold based
        on the full window lets the session hit a provider 400 before compaction
        fires (#43547). The percentage and the degenerate-window check below both
        operate on the effective input budget. ``max_tokens=None`` (provider
        default) conservatively assumes no reservation (full window).
        """
        effective_window = context_length - (max_tokens or 0)
        if effective_window <= 0:
            effective_window = context_length
        pct_value = int(effective_window * threshold_percent)
        floored = max(pct_value, MINIMUM_CONTEXT_LENGTH)
        # If flooring pushed the threshold to/over the effective window it can
        # never be reached. Trigger at 85% of the effective input budget so a
        # minimum-context model rides most of its budget before compacting
        # instead of wasting half.
        if effective_window > 0 and floored >= effective_window:
            return max(1, min(int(effective_window * ContextCompressorThresholdMixin._MIN_CTX_TRIGGER_RATIO),
                              effective_window - 1))
        return floored
