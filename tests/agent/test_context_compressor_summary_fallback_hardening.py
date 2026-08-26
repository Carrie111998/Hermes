"""Regression tests for PR #95231 summary-generation hardening.

Covers the reviewer findings on fix(compression): harden summary generation
against local/thinking backends:

1. The fallback-to-main output cap is derived from REMAINING context above a
   sane floor, not a hard ``max_tokens`` — so it never truncates a reasonable
   summary but never requests more output than the context can hold. When a
   dedicated aux model is in use, ``max_tokens`` stays unset (honouring the
   ``NO max_tokens`` invariant).

2. Falling back to raw ``reasoning_content`` as the compaction summary is
   bounded (truncated) and doesn't balloon the compressed prefix.

3. dict- and object-shaped messages normalize through ONE helper, so
   whitespace-only content falls back to reasoning_content in both shapes
   (no drift toward the empty-content RuntimeError).
"""

from typing import Any, Dict
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


def _compressor(**overrides: Any) -> ContextCompressor:
    """Minimal ContextCompressor with a stub context probe."""
    kwargs: Dict[str, Any] = dict(
        model="test/model",
        threshold_percent=0.85,
        protect_first_n=1,
        protect_last_n=1,
        quiet_mode=True,
    )
    kwargs.update(overrides)
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(**kwargs)


def _response(message):
    """Wrap a message (dict or object) in an LLM-response-shaped mock."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message = message
    return mock_response


def _capture_call_llm(captured, message):
    """Patch call_llm to record kwargs and return a fixed response."""
    def _fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response(message)
    return patch("agent.context_compressor.call_llm", side_effect=_fake_call_llm)


def test_fallback_to_main_bounds_output_cap_to_remaining_context():
    """FALLBACK (no aux model): max_tokens is set and bounded by context."""
    captured = {}
    with _capture_call_llm(captured, {"content": "summary body"}) as _:
        compressor = _compressor(config_context_length=10000)
        compressor.max_tokens = 32000
        out = compressor._generate_summary(
            [{"role": "user", "content": "some earlier turns"}], focus_topic="t"
        )
    assert out is not None, "expected a produced summary"
    assert "summary body" in out, f"expected summary body in output, got {out!r}"
    # A max_tokens IS set (fallback bounding active), and it must never exceed
    # the remaining-context bound (context - input-estimate), and must be >= floor.
    cap = captured["max_tokens"]
    assert cap is not None
    assert cap >= 2048, f"cap {cap} dropped below the sane floor"
    assert cap <= 10000, f"cap {cap} exceeds remaining context (10K)"
    assert cap <= 32000, f"cap {cap} exceeded configured max"


def test_aux_model_path_omits_max_tokens():
    """Dedicated aux model (NOT fallback): max_tokens stays unset (no truncation)."""
    captured = {}
    with _capture_call_llm(captured, {"content": "aux summary"}) as _:
        compressor = _compressor(summary_model_override="aux/model", provider="anthropic")
        out = compressor._generate_summary(
            [{"role": "user", "content": "turns"}], focus_topic="t"
        )
    assert out is not None, "expected aux summary to be produced"
    assert "aux summary" in out, f"expected aux summary in output, got {out!r}"
    assert "max_tokens" not in captured, (
        "dedicated aux path must omit max_tokens (NO max_tokens invariant)"
    )


def test_reasoning_content_fallback_when_content_empty():
    """Empty content -> falls back to reasoning_content, bounded."""
    captured = {}
    reasoning = "step 1 ... " + ("t" * 20000)  # long chain-of-thought
    msg = {"content": "", "reasoning_content": reasoning}
    with _capture_call_llm(captured, msg) as _:
        compressor = _compressor()
        out = compressor._generate_summary(
            [{"role": "user", "content": "turns"}], focus_topic="t"
        )
    assert out is not None and len(out) > 0, "expected reasoning fallback summary"
    # Bounded — the 20K-char CoT must have been truncated; if it had been used
    # whole, out would be ~22K (20K CoT + ~2K handoff preamble). The handoff
    # preamble inflates out, so bound against the full-trace size instead.
    assert len(out) < 15000, (
        f"reasoning fallback not truncated (full CoT leaked): {len(out)} chars"
    )
    assert reasoning not in out, "full unbounded reasoning trace leaked into summary"


def test_short_reasoning_content_fallback_not_truncated():
    """Short reasoning_content (within cap) is used whole."""
    captured = {}
    msg = {"content": "", "reasoning_content": "short reasoning trace"}
    with _capture_call_llm(captured, msg) as _:
        compressor = _compressor()
        out = compressor._generate_summary(
            [{"role": "user", "content": "turns"}], focus_topic="t"
        )
    assert out is not None, "expected a produced summary"
    assert "short reasoning trace" in out, f"got {out!r}"


def test_whitespace_content_falls_back_in_dict_and_object_shapes():
    """Finding #3: whitespace-only content must fall back to reasoning_content
    for BOTH dict- and object-shaped messages (no drift)."""
    reasoning = "dict reasoning"

    class _Msg:
        content = " "  # whitespace-only
        reasoning_content = "object reasoning"

    for i, message in enumerate([{"content": " ", "reasoning_content": reasoning}, _Msg()]):
        cap = {}
        with _capture_call_llm(cap, message) as _:
            compressor = _compressor()
            out = compressor._generate_summary(
                [{"role": "user", "content": "turns"}], focus_topic="t"
            )
        assert out is not None, "expected a produced summary"
        if i == 0:
            assert "dict reasoning" in out, f"dict branch got {out!r}"
        else:
            assert "object reasoning" in out, f"object branch got {out!r}"
