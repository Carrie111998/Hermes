"""Status surfaces must show the post-compaction context, not 0 or a stale value.

After a successful compaction, ``conversation_compression.py`` parks
``last_prompt_tokens`` at the -1 sentinel and records
``last_compression_rough_tokens``. Until the provider reports real usage for
the now-shorter conversation, that rough estimate is the only occupancy figure
available — and both status surfaces used to discard it:

  * the classic CLI status bar clamped the sentinel to 0 and rendered "0 / 0%",
    as if compaction had emptied the context rather than summarised it;
  * the TUI gateway emitted no gauge at all, which makes ``appChrome.tsx`` fall
    back to ``usage.total`` (cumulative session tokens, which compaction does
    NOT reduce) and drop the fill bar — so the reading got *bigger* right after
    a compaction.

The bridge must be gated on ``awaiting_real_usage_after_compression`` and not
on the rough estimate alone, which is never zeroed and would otherwise
resurrect a stale value on any later turn reporting ``prompt_tokens=0``.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import cli as cli_mod  # noqa: F401 - import side effects mirror the other CLI tests
from agent.context_compressor import (
    awaiting_post_compression_usage,
    context_gauge,
)
from cli import HermesCLI
from tui_gateway import server

CTX_MAX = 200_000
ROUGH = 120_000


def _compressor(*, last_prompt, awaiting, rough, compressions=1):
    return SimpleNamespace(
        last_prompt_tokens=last_prompt,
        context_length=CTX_MAX,
        compression_count=compressions,
        awaiting_real_usage_after_compression=awaiting,
        last_compression_rough_tokens=rough,
    )


def _agent(compressor):
    return SimpleNamespace(
        model="test-model",
        provider="custom",
        base_url="",
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        # Deliberately large: this is the cumulative figure the TUI falls back
        # to when the gateway omits context_max, and it must never be what the
        # user sees as "current context".
        session_total_tokens=1_900_000,
        session_api_calls=12,
        get_rate_limit_state=lambda: None,
        context_compressor=compressor,
    )


def _cli_snapshot(compressor):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "test-model"
    cli_obj.session_start = datetime.now() - timedelta(minutes=1)
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = _agent(compressor)
    return cli_obj._get_status_bar_snapshot()


# ── the shared predicate ──────────────────────────────────────────────────


class TestAwaitingPostCompressionUsage:
    def test_true_only_while_both_fields_are_set(self):
        assert awaiting_post_compression_usage(
            _compressor(last_prompt=-1, awaiting=True, rough=ROUGH)
        )

    def test_false_once_real_usage_cleared_the_flag(self):
        # update_from_response() clears the flag but never zeroes the estimate.
        assert not awaiting_post_compression_usage(
            _compressor(last_prompt=0, awaiting=False, rough=ROUGH)
        )

    def test_false_when_no_compaction_has_run(self):
        assert not awaiting_post_compression_usage(
            _compressor(last_prompt=0, awaiting=True, rough=0)
        )

    def test_tolerates_objects_missing_the_fields_entirely(self):
        # External context engines and older test stand-ins have neither field.
        assert not awaiting_post_compression_usage(SimpleNamespace())


# ── classic CLI status bar ────────────────────────────────────────────────


class TestCLIStatusBarAfterCompression:
    def test_sentinel_turn_shows_the_rough_estimate_not_zero(self):
        snap = _cli_snapshot(_compressor(last_prompt=-1, awaiting=True, rough=ROUGH))
        assert snap["context_tokens"] == ROUGH
        assert snap["context_percent"] == 60
        assert snap["context_estimated"] is True

    def test_normal_turn_uses_real_usage(self):
        snap = _cli_snapshot(_compressor(last_prompt=50_000, awaiting=False, rough=ROUGH))
        assert snap["context_tokens"] == 50_000
        assert snap["context_percent"] == 25
        assert snap["context_estimated"] is False

    def test_later_zero_usage_turn_does_not_resurrect_the_estimate(self):
        # A provider (or a proxied stream without usage) reporting 0 long after
        # the compaction must read as 0, not as the old rough estimate.
        snap = _cli_snapshot(_compressor(last_prompt=0, awaiting=False, rough=ROUGH))
        assert snap["context_tokens"] == 0
        assert snap["context_percent"] == 0

    def test_sentinel_never_renders_verbatim(self):
        snap = _cli_snapshot(_compressor(last_prompt=-1, awaiting=False, rough=0))
        assert snap["context_tokens"] == 0
        assert snap["context_percent"] == 0


# ── TUI gateway usage payload ─────────────────────────────────────────────


class TestGatewayUsageAfterCompression:
    def test_sentinel_turn_emits_a_gauge_from_the_rough_estimate(self):
        usage = server._get_usage(
            _agent(_compressor(last_prompt=-1, awaiting=True, rough=ROUGH))
        )
        assert usage["context_used"] == ROUGH
        assert usage["context_max"] == CTX_MAX
        assert usage["context_percent"] == 60
        assert usage["context_estimated"] is True
        # Without context_max the TUI would show this cumulative total instead.
        assert usage["total"] == 1_900_000

    def test_normal_turn_uses_real_usage(self):
        usage = server._get_usage(
            _agent(_compressor(last_prompt=50_000, awaiting=False, rough=ROUGH))
        )
        assert usage["context_used"] == 50_000
        assert usage["context_percent"] == 25
        assert "context_estimated" not in usage

    def test_external_context_engine_still_emits_no_gauge(self):
        # #50421: an engine that doesn't track per-window occupancy must not get
        # a fabricated gauge — it sets neither compaction field.
        usage = server._get_usage(
            _agent(
                SimpleNamespace(
                    last_prompt_tokens=0,
                    context_length=CTX_MAX,
                    compression_count=0,
                )
            )
        )
        assert "context_used" not in usage
        assert "context_max" not in usage
        assert "context_percent" not in usage

    def test_later_zero_usage_turn_does_not_resurrect_the_estimate(self):
        usage = server._get_usage(
            _agent(_compressor(last_prompt=0, awaiting=False, rough=ROUGH))
        )
        assert "context_used" not in usage
        assert "context_max" not in usage


# ── shared gauge math ─────────────────────────────────────────────────────


class TestContextGauge:
    """One helper owns the clamp + rounding for both surfaces."""

    def test_ordinary_reading(self):
        assert context_gauge(50_000, 200_000) == (50_000, 25)

    def test_rough_estimate_above_the_window_is_clamped_to_it(self):
        # The rough estimator intentionally over-counts schema-heavy requests,
        # so a post-compaction estimate can exceed context_length. Reporting
        # "180k / 150k" next to a 100%-clamped bar made the two halves of the
        # read-out contradict each other; clamp the used figure too.
        assert context_gauge(180_000, 150_000) == (150_000, 100)

    def test_zero_window_yields_zero_percent_not_a_zero_division(self):
        assert context_gauge(1_000, 0) == (1_000, 0)

    def test_negative_and_none_are_floored(self):
        assert context_gauge(-5, 200_000) == (0, 0)
        assert context_gauge(None, 200_000) == (0, 0)


# ── the bridge is bounded to one turn ─────────────────────────────────────


class TestBridgeIsBoundedToOneTurn:
    """A usage-less provider must not pin the gauge to a stale estimate.

    Both guarantees live in the real compressor, so exercise the real class
    rather than a stand-in.
    """

    def _real_compressor(self):
        """A really-constructed compressor parked in the post-compaction state.

        Constructed through __init__ rather than assembled attribute-by-attribute
        so the test exercises the same object the agent loop hands to the status
        surfaces (threshold_tokens is a lazy property with several dependencies).
        """
        from agent.context_compressor import ContextCompressor

        comp = ContextCompressor(model="test-model", config_context_length=CTX_MAX)
        # What conversation_compression.py leaves behind after a compaction.
        comp.last_prompt_tokens = -1
        comp.awaiting_real_usage_after_compression = True
        comp.last_compression_rough_tokens = ROUGH
        comp.compression_count = 1
        return comp

    def test_response_with_no_usage_still_clears_the_flag(self):
        comp = self._real_compressor()
        assert awaiting_post_compression_usage(comp)
        # conversation_loop.py calls update_from_response({}) exactly when a
        # response carries no usage while this flag is armed.
        comp.update_from_response({})
        assert not awaiting_post_compression_usage(comp), (
            "a usage-less response must consume the pending flag, otherwise the "
            "estimate would pin the gauge for the rest of the session"
        )

    def test_real_usage_clears_the_flag(self):
        comp = self._real_compressor()
        comp.update_from_response({"prompt_tokens": 140_000, "completion_tokens": 10})
        assert not awaiting_post_compression_usage(comp)
        # The estimate itself is deliberately NOT zeroed — it stays available as
        # a preflight baseline — which is why the predicate needs both fields.
        assert comp.last_compression_rough_tokens == ROUGH
