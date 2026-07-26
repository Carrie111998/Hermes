"""Frozen, network-free non-LLM cost rate cards."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetellRate:
    usd_per_minute: float = 0.31


RETELL = RetellRate()


@dataclass(frozen=True)
class PerplexityRate:
    usd_per_1k_input_small: float = 0.001
    usd_per_1k_output_small: float = 0.001
    usd_per_1k_input_large: float = 0.005
    usd_per_1k_output_large: float = 0.005
    large_threshold_input_tokens: int = 8_000


PERPLEXITY = PerplexityRate()


def retell_usd(minutes: float) -> float:
    value = float(minutes)
    if value < 0:
        raise ValueError("minutes must be non-negative")
    return round(value * RETELL.usd_per_minute, 6)


def perplexity_usd(input_tokens: int, output_tokens: int) -> float:
    input_count = int(input_tokens)
    output_count = int(output_tokens)
    if input_count < 0 or output_count < 0:
        raise ValueError("token counts must be non-negative")
    if input_count > PERPLEXITY.large_threshold_input_tokens:
        input_rate = PERPLEXITY.usd_per_1k_input_large
        output_rate = PERPLEXITY.usd_per_1k_output_large
    else:
        input_rate = PERPLEXITY.usd_per_1k_input_small
        output_rate = PERPLEXITY.usd_per_1k_output_small
    return round(
        (input_count / 1000.0) * input_rate
        + (output_count / 1000.0) * output_rate,
        6,
    )


__all__ = [
    "PERPLEXITY",
    "RETELL",
    "PerplexityRate",
    "RetellRate",
    "perplexity_usd",
    "retell_usd",
]
