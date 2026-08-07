"""Adaptive context-governor behavior.

The governor starts with an efficient base budget, grows only when the
protected working set requires more room, and never crosses the model route's
hard safety ratio.  It is opt-in so existing compression policy remains byte-
compatible when disabled.
"""

from agent.context_compressor import ContextCompressor


def _compressor(*, context_length: int = 1_000_000, **kwargs) -> ContextCompressor:
    return ContextCompressor(
        model="test/adaptive-model",
        config_context_length=context_length,
        quiet_mode=True,
        adaptive_context=True,
        adaptive_base_tokens=231_200,
        adaptive_headroom_ratio=0.20,
        adaptive_step_tokens=65_536,
        adaptive_hard_ratio=0.85,
        **kwargs,
    )


def test_small_working_set_uses_efficient_base_budget():
    compressor = _compressor()

    assert compressor.adaptive_threshold_for_working_set(100_000) == 231_200


def test_large_working_set_raises_budget_in_steps():
    compressor = _compressor()

    assert compressor.adaptive_threshold_for_working_set(400_000) == 524_288


def test_budget_never_crosses_route_safety_limit():
    compressor = _compressor()

    assert compressor.adaptive_threshold_for_working_set(700_000) == 850_000


def test_multi_million_window_can_grow_beyond_fixed_half_million_cap():
    compressor = _compressor(context_length=4_100_000)

    assert compressor.adaptive_threshold_for_working_set(1_000_000) == 1_245_184


def test_refresh_has_hysteresis_until_explicit_reset():
    compressor = _compressor()

    raised = compressor.refresh_adaptive_threshold(
        [], request_tokens=500_000, protected_working_set_tokens=400_000
    )
    lowered_attempt = compressor.refresh_adaptive_threshold(
        [], request_tokens=150_000, protected_working_set_tokens=100_000
    )

    assert raised == 524_288
    assert lowered_attempt == 524_288

    compressor.reset_adaptive_threshold()
    assert compressor.threshold_tokens == 231_200


def test_static_absolute_cap_remains_an_upper_bound():
    compressor = _compressor(threshold_tokens_cap=300_000)

    assert compressor.adaptive_threshold_for_working_set(700_000) == 300_000


def test_disabled_governor_preserves_existing_ratio_threshold():
    compressor = ContextCompressor(
        model="test/static-model",
        config_context_length=1_000_000,
        threshold_percent=0.50,
        quiet_mode=True,
        adaptive_context=False,
    )

    assert compressor.threshold_tokens == 500_000


def test_active_turn_suffix_is_part_of_protected_working_set():
    compressor = _compressor(context_length=4_100_000, protect_first_n=0, protect_last_n=3)
    # A current turn can contain more messages than protect_last_n.  Everything
    # from the latest real user instruction through its tool loop must count.
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 800_000},
        {"role": "assistant", "content": "continuing"},
        {"role": "assistant", "content": "working"},
    ]

    protected = compressor.estimate_protected_working_set_tokens(
        messages, request_tokens=250_000
    )

    # The 800K-char tool result is ~200K tokens and lies outside the final
    # three messages, but it belongs to the current user turn and must count.
    assert protected >= 200_000
