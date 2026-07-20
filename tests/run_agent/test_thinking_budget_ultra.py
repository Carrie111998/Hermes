"""Regression test: THINKING_BUDGET must define an "ultra" tier.

The Hermes ``THINKING_BUDGET`` dict maps ``reasoning_effort`` levels to
Anthropic thinking-mode token budgets. Operators that set
``agent.reasoning_effort: ultra`` (Basti's default in
~/.hermes/config.yaml) previously fell through to the 8000-token
"medium" budget because the dict had no "ultra" key and
``THINKING_BUDGET.get("ultra")`` returned ``None``, which the budget
allocator then defaulted to medium. This silently halved the reasoning
headroom for extended multi-step tool chains.

This test guards the addition of an explicit ``"ultra"`` key. The
*value* is not contractually pinned — it can move with Anthropic's
4.7+ adaptive-thinking ceilings — but the *key* must exist and must
be strictly larger than the next-highest tier (``xhigh``) so that
``reasoning_effort: ultra`` actually grants more budget than
``xhigh``.

Refs: H-11 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

import pytest

from agent.anthropic_adapter import THINKING_BUDGET


class TestThinkingBudgetUltra:
    """THINKING_BUDGET must expose an "ultra" tier that beats xhigh."""

    def test_ultra_key_present(self) -> None:
        assert "ultra" in THINKING_BUDGET, (
            "THINKING_BUDGET must define an 'ultra' tier so that "
            "reasoning_effort: ultra doesn't silently fall through to "
            "medium (8000 tokens). See H-11."
        )

    def test_ultra_value_positive_int(self) -> None:
        assert isinstance(THINKING_BUDGET["ultra"], int)
        assert THINKING_BUDGET["ultra"] > 0

    def test_ultra_strictly_above_xhigh(self) -> None:
        """ultra must grant MORE budget than xhigh, otherwise the
        effort level is meaningless (same cost, weaker guarantee)."""
        assert "xhigh" in THINKING_BUDGET
        assert THINKING_BUDGET["ultra"] > THINKING_BUDGET["xhigh"], (
            f"ultra ({THINKING_BUDGET['ultra']}) must exceed "
            f"xhigh ({THINKING_BUDGET['xhigh']}) — otherwise "
            "reasoning_effort: ultra has no effect."
        )

    def test_all_known_tiers_present(self) -> None:
        """Sanity: the canonical effort ladder is intact."""
        expected = {"ultra", "xhigh", "high", "medium", "low"}
        assert expected.issubset(THINKING_BUDGET.keys()), (
            f"Missing tiers: {expected - THINKING_BUDGET.keys()}"
        )

    def test_tier_ordering_monotonic_decreasing(self) -> None:
        """ultra > xhigh > high > medium > low (strict)."""
        tiers = ["ultra", "xhigh", "high", "medium", "low"]
        for higher, lower in zip(tiers, tiers[1:]):
            assert THINKING_BUDGET[higher] > THINKING_BUDGET[lower], (
                f"{higher} ({THINKING_BUDGET[higher]}) must exceed "
                f"{lower} ({THINKING_BUDGET[lower]})"
            )


@pytest.mark.parametrize(
    "effort,expected_min",
    [
        ("ultra", 32768),
        ("xhigh", 16384),
        ("high", 8192),
        ("medium", 4096),
        ("low", 1024),
    ],
)
def test_effort_resolves_to_at_least_minimum_budget(effort: str, expected_min: int) -> None:
    """Every tier must allocate at least the documented minimum.

    These minimums are conservative floors — actual values can grow as
    Anthropic raises ceilings, but they must never regress below the
    values that previous Hermes versions exposed.
    """
    assert THINKING_BUDGET[effort] >= expected_min, (
        f"THINKING_BUDGET[{effort!r}] = {THINKING_BUDGET[effort]} "
        f"regressed below documented minimum {expected_min}"
    )
