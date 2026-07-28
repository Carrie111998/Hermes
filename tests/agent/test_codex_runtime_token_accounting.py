"""Regression test for #hermes-context-overrun (7th recurrence, 2026-07-27,
423K/258.4K = 164% gauge reading with zero real context-length-exceeded API
errors in agent.log for that window).

Root cause: ``_record_codex_app_server_usage`` built ``prompt_tokens`` as
``inputTokens + cachedInputTokens``, but the codex app-server wire protocol
reports ``inputTokens`` ALREADY INCLUSIVE of ``cachedInputTokens`` (verified
against the installed codex-cli's own
``codex app-server generate-json-schema`` output and the upstream codex-rs
test fixture in ``codex-rs/codex-api/src/sse/responses.rs::
parses_cache_write_token_usage``: input_tokens=100, cached_tokens=40 (a
SUBSET of the 100), output_tokens=10 -> total_tokens=110, i.e. 100+10, never
100+40+10=150). Adding the cached share back on top double-counted it,
inflating ``last_real_prompt_tokens`` (the number both the live status gauge
and turn_context.py's preflight/compaction backstop compare against the
context window) by up to ~2x once a session's cache-hit ratio climbs, which
is exactly what happens as a long session's context grows.

This test drives the real accounting function against that exact upstream
fixture so a future edit that reintroduces the double-count fails loudly
here instead of silently re-inflating the live gauge.
"""
from types import SimpleNamespace

from agent import codex_runtime


class _FakeCompressor:
    def __init__(self):
        self.context_length = 258_400
        self.threshold_tokens = 193_800
        self.threshold_percent = 0.75
        self.max_tokens = None
        self.last_prompt_tokens = 0
        self.last_real_prompt_tokens = 0
        self.last_total_tokens = 0
        self.last_completion_tokens = 0

    def update_from_response(self, usage):
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)
        if self.last_prompt_tokens > 0:
            self.last_real_prompt_tokens = self.last_prompt_tokens

    def _compute_threshold_tokens(self, *a, **k):
        return self.threshold_tokens

    def _apply_threshold_tokens_cap(self):
        pass


def _agent_with_compressor():
    return SimpleNamespace(
        session_api_calls=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status=None,
        session_cost_source=None,
        context_compressor=_FakeCompressor(),
        model="gpt-5.6-sol",
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex/",
        api_key="",
        _session_db=None,
        session_id=None,
    )


def test_no_cached_token_double_count_matches_upstream_fixture():
    """codex-rs's own ``parses_cache_write_token_usage`` fixture: inputTokens
    is already inclusive of cachedInputTokens, so the real prompt size is 100
    (not 140) and the real total is 110 (not 150)."""
    agent = _agent_with_compressor()
    turn = SimpleNamespace(
        token_usage_last={
            "inputTokens": 100,
            "cachedInputTokens": 40,
            "outputTokens": 10,
            "reasoningOutputTokens": 5,
            "totalTokens": 110,
        },
        model_context_window=258_400,
    )

    result = codex_runtime._record_codex_app_server_usage(agent, turn)

    assert result["prompt_tokens"] == 100, (
        "prompt_tokens double-counted cachedInputTokens on top of the "
        "already-inclusive inputTokens (regressed to the "
        "#hermes-context-overrun bug)"
    )
    assert result["total_tokens"] == 110
    assert agent.context_compressor.last_real_prompt_tokens == 100, (
        "last_real_prompt_tokens is what the live gauge and the "
        "preflight/compaction backstop both read -- an inflated value here "
        "reproduces the 423K/258.4K false-overrun incident"
    )


def test_high_cache_hit_ratio_does_not_inflate_toward_double():
    """A realistic high-cache-hit-ratio turn (the pattern that produced the
    7th recurrence): without the fix, prompt_tokens would be
    86919 + 77568 = 164487 against a 258400 window (63%, still under, but
    already meaningfully inflated); with the fix it must equal the real
    inputTokens, 86919 (34%)."""
    agent = _agent_with_compressor()
    turn = SimpleNamespace(
        token_usage_last={
            "inputTokens": 86919,
            "cachedInputTokens": 77568,
            "outputTokens": 400,
            "reasoningOutputTokens": 120,
            "totalTokens": 87319,
        },
        model_context_window=258_400,
    )

    result = codex_runtime._record_codex_app_server_usage(agent, turn)

    assert result["prompt_tokens"] == 86919
    assert result["prompt_tokens"] != 86919 + 77568
