"""claude-opus-5 must resolve pricing like its siblings (#100848)."""

import sys

sys.path.insert(0, r"C:\Users\salma\dev\hermes-agent")

from agent.usage_pricing import (  # noqa: E402
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
)


def test_claude_opus_5_has_pricing_entry():
    entry = get_pricing_entry("claude-opus-5", provider="anthropic")
    assert entry is not None, "claude-opus-5 must have an official-docs row"
    assert str(entry.input_cost_per_million) == "5.00"
    assert str(entry.output_cost_per_million) == "25.00"
    assert str(entry.cache_read_cost_per_million) == "0.50"
    assert str(entry.cache_write_cost_per_million) == "6.25"


def test_claude_opus_5_estimates_instead_of_unknown():
    usage = CanonicalUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    result = estimate_usage_cost(
        model_name="claude-opus-5",
        usage=usage,
        provider="anthropic",
    )
    assert result.status == "estimated", (
        f"expected estimated, got {result.status} — $0/unknown is the #100848 bug"
    )
    assert result.amount_usd is not None
    # 1M input ($5) + 1M output ($25) = $30
    assert abs(float(result.amount_usd) - 30.00) < 0.01


def test_cache_components_price_at_published_rates():
    usage = CanonicalUsage(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=2_000_000,   # $0.50/M -> $1.00
        cache_write_tokens=2_000_000,  # $6.25/M -> $12.50 (5m TTL rate)
    )
    result = estimate_usage_cost(
        model_name="claude-opus-5",
        usage=usage,
        provider="anthropic",
    )
    assert result.status == "estimated"
    assert abs(float(result.amount_usd) - 13.50) < 0.01


def test_prefixed_form_resolves():
    """anthropic/claude-opus-5 must resolve to the same entry via the
    Anthropic prefix-strip normalization path."""
    entry = get_pricing_entry("anthropic/claude-opus-5", provider="anthropic")
    assert entry is not None
    assert str(entry.input_cost_per_million) == "5.00"


def test_dot_notation_form_matches_sibling_behavior():
    """claude-opus-5.0 (dot form) is NOT normalized to the dash key today —
    the same is true for the existing claude-opus-4-6.0 sibling. Pinning the
    current sibling-consistent behavior; if the dot-notation gap is ever
    fixed for the 4-x family, this test reminds the fixer to cover opus-5
    at the same time."""
    entry = get_pricing_entry("claude-opus-4-6.0", provider="anthropic")
    ours = get_pricing_entry("claude-opus-5.0", provider="anthropic")
    # Whatever the behavior is, opus-5 must not be worse than its sibling.
    assert (ours is None) == (entry is None), (
        "opus-5 dot-notation behavior diverged from the 4-6 sibling"
    )
