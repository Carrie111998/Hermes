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
from agent.context_compressor import awaiting_post_compression_usage
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

    def test_normal_turn_uses_real_usage(self):
        snap = _cli_snapshot(_compressor(last_prompt=50_000, awaiting=False, rough=ROUGH))
        assert snap["context_tokens"] == 50_000
        assert snap["context_percent"] == 25

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
        # Without context_max the TUI would show this cumulative total instead.
        assert usage["total"] == 1_900_000

    def test_normal_turn_uses_real_usage(self):
        usage = server._get_usage(
            _agent(_compressor(last_prompt=50_000, awaiting=False, rough=ROUGH))
        )
        assert usage["context_used"] == 50_000
        assert usage["context_percent"] == 25

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
