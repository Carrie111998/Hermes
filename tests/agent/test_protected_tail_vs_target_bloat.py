"""Regression test for the protected-tail vs target bloat failure mode.

A tool-heavy session can pin hundreds of thousands of tokens inside the
last-N-message protected tail (message-count protection, not size). When that
tail alone exceeds threshold*target_ratio, compaction mathematically cannot
reach its target: every pass only shrinks the small compressible middle,
reports "insufficient progress", and retries. Legacy mode mitigates with a
pressure demotion pass (#61932); lean mode (tail_mode="lean") clamps the tail
to a hard token band so the target stays reachable.

These tests pin both behaviors at the unit level.
"""

import pytest

from agent.context_compressor import (
    ContextCompressor,
    LEAN_TAIL_CAP_TOKENS,
    LEAN_TAIL_FLOOR_TOKENS,
)


def _bulky_tool_session(num_bulky: int, chars_per_bulky: int = 160_000):
    """Build a session whose last messages are huge tool outputs.

    Roughly `chars_per_bulky / 4` tokens each; num_bulky=12 gives ~480K
    tokens of recent-tail payload — far above a lean cap and comparable to
    the legacy 0.20x262144 = ~52K soft budget.
    """
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(3):
        msgs.append({"role": "user", "content": f"kickoff question {i}"})
        msgs.append({"role": "assistant", "content": f"answer {i}"})
    for i in range(num_bulky):
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command": "cat big.log"}',
                },
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": ("x" * chars_per_bulky) + f" unique-{i}",
        })
    msgs.append({"role": "user", "content": "what did the log say?"})
    return msgs


def _estimate_tokens(msgs):
    from agent.context_compressor import _estimate_msg_budget_tokens
    return sum(_estimate_msg_budget_tokens(m) for m in msgs)


def _legacy_pressure_prune():
    comp = ContextCompressor(
        "m", threshold_percent=0.85, protect_first_n=3, protect_last_n=10,
        summary_target_ratio=0.2, quiet_mode=True,
    )
    # threshold_tokens for a 262,144 window at 0.85 -> tail budget = 0.2 * that
    comp.update_model("m", context_length=262_144)
    return comp


def test_legacy_tail_budget_is_ratio_scaled():
    """Legacy tail budget = threshold * target_ratio (the 0.20x-window rule)."""
    comp = _legacy_pressure_prune()
    assert comp.threshold_tokens == int(262_144 * 0.85)
    assert comp.tail_token_budget == int(comp.threshold_tokens * 0.20)
    # sanity: that budget is what makes the target unreachable when the
    # message-count floor pins bulky tool outputs inside it
    assert comp.tail_token_budget > LEAN_TAIL_CAP_TOKENS


def test_lean_tail_budget_clamped_to_band():
    """Lean mode clamps the verbatim tail to [10K, 25K] regardless of window."""
    for window in (128_000, 262_144, 1_000_000):
        comp = ContextCompressor(
            "m", threshold_percent=0.85, quiet_mode=True, tail_mode="lean",
        )
        comp.update_model("m", context_length=window)
        expected = max(LEAN_TAIL_FLOOR_TOKENS,
                       min(LEAN_TAIL_CAP_TOKENS, int(window * 0.025)))
        assert comp.tail_token_budget == expected


def test_bulky_recent_tools_exceed_target_under_message_count_protection():
    """The failure shape: last-10-messages tail >> threshold*target_ratio."""
    comp = _legacy_pressure_prune()
    msgs = _bulky_tool_session(num_bulky=12)
    tail = msgs[-11:]  # user ask + 5 tool pairs ~= protected count region
    assert _estimate_tokens(tail) > comp.tail_token_budget * 2
    # and the whole point: tail alone dwarfs the post-compression target
    target = int(comp.threshold_tokens * comp.summary_target_ratio)
    assert _estimate_tokens(tail) > target * 2


def test_pressure_pass_demotes_bulky_tools_inside_protected_region():
    """Legacy pressure demotion (#61932): oversized completed tool bodies in
    the protected region get demoted while a short recent floor stays
    verbatim — the deterministic backstop before lean mode existed."""
    comp = _legacy_pressure_prune()
    msgs = _bulky_tool_session(num_bulky=12)
    pruned, count = comp._prune_old_tool_results(
        msgs,
        protect_tail_count=comp.protect_last_n,
        protect_tail_tokens=comp.tail_token_budget,
        min_prune_chars=2000,
    )
    assert count > 0
    assert _estimate_tokens(pruned) < _estimate_tokens(msgs)


def test_unknown_tail_mode_falls_back_to_legacy():
    comp = ContextCompressor(
        "m", threshold_percent=0.85, quiet_mode=True, tail_mode="bogus",
    )
    assert comp.tail_mode == "legacy"


def test_lean_mode_survives_update_model():
    """Bug fix: update_model() used to clobber the lean clamped band with the
    ratio formula, silently reverting tail_mode=lean to a legacy-sized tail
    on every model switch / fallback activation."""
    comp = ContextCompressor(
        "m", threshold_percent=0.85, quiet_mode=True, tail_mode="lean",
    )
    comp.update_model("m", context_length=262_144)
    expected = max(LEAN_TAIL_FLOOR_TOKENS,
                   min(LEAN_TAIL_CAP_TOKENS, int(262_144 * 0.025)))
    assert comp.tail_token_budget == expected

    # switching windows must re-derive, not stick or clobber
    comp.update_model("m", context_length=1_000_000)
    expected_1m = max(LEAN_TAIL_FLOOR_TOKENS,
                      min(LEAN_TAIL_CAP_TOKENS, int(1_000_000 * 0.025)))
    assert comp.tail_token_budget == expected_1m


def test_legacy_mode_still_recalibrates_budget_on_model_switch():
    """Guard: the fix must not break legacy budget recalculation."""
    comp = ContextCompressor(
        "m", threshold_percent=0.50, quiet_mode=True,
    )
    assert comp.tail_mode == "legacy"
    comp.update_model("m", context_length=10_000)
    assert comp.tail_token_budget == int(comp.threshold_tokens * 0.20)


def test_no_second_ratio_writer_for_lean_mode():
    """Reviewer pin (#92738): any SECOND writer applying the legacy ratio
    formula would reintroduce the lean-revert bug through a side door. The
    only production writer is update_model() — simulate its two callers
    (explicit switch + fallback activation) and assert lean stays window-
    derived; also lock invalidate_tail_budget() as the public cache hook."""
    comp = ContextCompressor(
        "m", threshold_percent=0.85, quiet_mode=True, tail_mode="lean",
    )
    # fallback activation re-runs update_model with the same model id
    for _ in range(2):
        comp.update_model("m", context_length=32_768)
    expected = max(LEAN_TAIL_FLOOR_TOKENS,
                   min(LEAN_TAIL_CAP_TOKENS, int(32_768 * 0.025)))
    assert comp.tail_token_budget == expected

    # public invalidation hook recomputes identically (no private-attr poke)
    comp.invalidate_tail_budget()
    assert comp._tail_token_budget is None
    assert comp.tail_token_budget == expected

    # legacy mode still writes through the setter (the ratio path) — the
    # asymmetry is the contract: lean derives, legacy assigns.
    legacy = ContextCompressor("m", threshold_percent=0.50, quiet_mode=True)
    legacy.update_model("m", context_length=10_000)
    assert legacy._tail_token_budget == int(legacy.threshold_tokens * 0.20)
