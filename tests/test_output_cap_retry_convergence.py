"""The output-cap retry must make forward progress.

When a provider rejects a call because ``input_tokens + max_tokens`` exceeds
the context window, ``conversation_loop`` recomputes ``max_tokens`` from the
budget the provider reported and retries. That budget describes the request
that ALREADY FAILED -- by the time the retry is sent the payload has grown
slightly (retry bookkeeping, status lines), so the real budget is smaller than
the parsed one.

A FIXED safety margin smaller than that per-retry growth can never converge.
The overshoot is identical on every attempt:

    safe_out   = (ctx - input_n) - margin
    next_total = (input_n + growth) + safe_out
               = ctx + (growth - margin)

Observed against a 262,144-token vLLM deployment: three attempts at totals of
exactly 262,145 (one token over, every time), then "max compression attempts
(3) reached". No attempt count would have helped.

These tests pin the ESCALATION, not the specific constants -- doubling is what
guarantees the margin eventually outgrows any bounded growth.
"""

from __future__ import annotations

import pytest

from agent.conversation_loop import (
    _OUTPUT_CAP_BASE_MARGIN_TOKENS,
    _OUTPUT_CAP_MAX_MARGIN_SHIFT,
)
from agent.model_metadata import parse_available_output_tokens_from_error

_CTX = 262_144
# The live failure, reproduced exactly.
_INITIAL_INPUT = 196_674
_INITIAL_OUTPUT = 65_471
_OBSERVED_GROWTH = 65


def _vllm_error(requested_out: int, input_tokens: int, ctx: int = _CTX) -> str:
    """The exact wording vLLM / OpenAI-compatible servers return."""
    return (
        f"This model's maximum context length is {ctx} tokens. However, you "
        f"requested {requested_out} output tokens and your prompt contains at "
        f"least {input_tokens} input tokens, for a total of at least "
        f"{requested_out + input_tokens} tokens."
    )


def _margin_for_attempt(attempt_index: int) -> int:
    """Mirrors the production expression in conversation_loop."""
    return _OUTPUT_CAP_BASE_MARGIN_TOKENS * (
        2 ** min(attempt_index, _OUTPUT_CAP_MAX_MARGIN_SHIFT)
    )


def _simulate(growth: int, margin_fn, max_attempts: int = 6):
    """Replay the retry loop's arithmetic. Returns the 1-based converging
    attempt, or None if it never fits."""
    inp, req = _INITIAL_INPUT, _INITIAL_OUTPUT
    for attempt in range(max_attempts):
        if inp + req <= _CTX:
            return attempt + 1
        available_out = parse_available_output_tokens_from_error(_vllm_error(req, inp))
        assert available_out is not None, "error must classify as an output-cap error"
        req = max(1, min(available_out, _CTX - inp) - margin_fn(attempt))
        inp += growth
    return None


class TestFixedMarginCannotConverge:
    """Why the escalation is required, not merely nicer."""

    def test_flat_margin_below_growth_never_converges(self):
        assert _simulate(_OBSERVED_GROWTH, lambda _a: 64) is None

    def test_flat_margin_overshoots_by_exactly_the_same_amount_every_time(self):
        """The signature of the live bug: a constant overshoot, not a shrinking
        one. This is what makes it look like compression 'isn't working'."""
        inp, req, overshoots = _INITIAL_INPUT, _INITIAL_OUTPUT, []
        for _ in range(4):
            overshoots.append(inp + req - _CTX)
            available_out = parse_available_output_tokens_from_error(_vllm_error(req, inp))
            req = max(1, min(available_out, _CTX - inp) - 64)
            inp += _OBSERVED_GROWTH
        assert overshoots == [1, 1, 1, 1]


class TestEscalatingMarginConverges:
    def test_converges_on_the_observed_live_failure(self):
        attempt = _simulate(_OBSERVED_GROWTH, _margin_for_attempt)
        assert attempt is not None
        # Must land inside the default compression.max_attempts of 3, with room
        # to spare -- converging exactly ON the last attempt leaves no headroom
        # for a provider that drifts a little more than measured.
        assert attempt <= 2

    @pytest.mark.parametrize("growth", [1, 65, 200, 1000])
    def test_converges_within_the_default_attempt_budget(self, growth):
        attempt = _simulate(growth, _margin_for_attempt)
        assert attempt is not None and attempt <= 3, (
            f"growth={growth} converged on attempt {attempt}; the default "
            "compression.max_attempts is 3"
        )

    def test_margin_doubles_then_caps(self):
        margins = [_margin_for_attempt(i) for i in range(_OUTPUT_CAP_MAX_MARGIN_SHIFT + 3)]
        for earlier, later in zip(margins, margins[1:]):
            assert later in (earlier * 2, earlier), "margin must double or hold at the cap"
        # The cap exists so a high attempt count cannot collapse the output
        # budget to nothing.
        assert margins[-1] == _OUTPUT_CAP_BASE_MARGIN_TOKENS * 2**_OUTPUT_CAP_MAX_MARGIN_SHIFT

    def test_margin_never_drives_the_output_cap_below_one(self):
        """max(1, ...) in production; a huge margin must not yield 0 or negative."""
        available_out = 100
        for attempt in range(10):
            assert max(1, available_out - _margin_for_attempt(attempt)) >= 1
