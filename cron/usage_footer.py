"""Per-execution native LLM usage accounting for cron delivery.

The accumulator is held in a ContextVar, never in module-global mutable state.
A scheduler run activates it around its agent work; the core request path records
provider-normalized usage into that exact context.  Interactive calls have no
active scope and are deliberately ignored.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterator

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

_ACTIVE_CRON_USAGE: ContextVar["CronRunUsage | None"] = ContextVar(
    "active_cron_usage", default=None
)


def _tokens(value: int) -> str:
    """Compact, deterministic token formatting used only for the delivery footer."""
    value = int(value)
    if abs(value) < 1_000:
        return str(value)
    return f"{value / 1_000:.1f}k"


def _usd(value: Decimal) -> str:
    """Retain useful precision for cheap requests without implying provider cost."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"


@dataclass
class CronRunUsage:
    """One cron execution's native request usage and local pricing state."""

    usage: CanonicalUsage = field(
        default_factory=lambda: CanonicalUsage(request_count=0)
    )
    routes: set[tuple[str, str]] = field(default_factory=set)
    known_cost: Decimal = Decimal("0")
    estimated_calls: int = 0
    unavailable_calls: int = 0

    def record(
        self,
        usage: CanonicalUsage | None,
        provider: str,
        model: str,
        base_url: str = "",
    ) -> None:
        """Record exactly one post-request observation without raising into a run."""
        self.usage += usage or CanonicalUsage()
        self.routes.add(((provider or "").strip(), (model or "").strip()))
        if usage is None:
            self.unavailable_calls += 1
            return
        try:
            estimated = estimate_usage_cost(
                model, usage, provider=provider, base_url=base_url,
            )
        except Exception:
            self.unavailable_calls += 1
            return
        if estimated.amount_usd is None:
            self.unavailable_calls += 1
            return
        self.known_cost += estimated.amount_usd
        self.estimated_calls += 1

    @property
    def call_count(self) -> int:
        return self.usage.request_count

    def footer(self) -> str:
        """Format a footer only when at least one actual LLM request was observed.

        Total is intentionally ``input + output``.  Canonical reasoning is
        commonly a subset of output, while cache buckets are provider input
        sub-buckets, so neither is added again.
        """
        if not self.call_count:
            return ""
        parts = [
            f"{_tokens(self.usage.input_tokens)} input",
            f"{_tokens(self.usage.output_tokens)} output",
        ]
        if self.usage.reasoning_tokens:
            parts.append(f"{_tokens(self.usage.reasoning_tokens)} reasoning")
        parts.append(f"{_tokens(self.usage.input_tokens + self.usage.output_tokens)} total")
        lines = ["──", "Usage: " + " · ".join(parts)]
        cache_parts = []
        if self.usage.cache_read_tokens:
            cache_parts.append(f"{_tokens(self.usage.cache_read_tokens)} read")
        if self.usage.cache_write_tokens:
            cache_parts.append(f"{_tokens(self.usage.cache_write_tokens)} write")
        if cache_parts:
            lines.append("Cache: " + " · ".join(cache_parts))
        calls = f"{self.call_count} LLM call" + ("" if self.call_count == 1 else "s")
        if self.estimated_calls == self.call_count:
            cost = f"Estimated cost: ${_usd(self.known_cost)}"
        elif self.estimated_calls:
            cost = f"Estimated cost incomplete: ${_usd(self.known_cost)}"
        else:
            cost = "Estimated cost unavailable"
        suffix = [calls]
        if len(self.routes) > 1:
            suffix.append(f"Routes: {len(self.routes)}")
        lines.append(cost + " · " + " · ".join(suffix))
        return "\n".join(lines)


@contextmanager
def activate_cron_usage() -> Iterator[CronRunUsage]:
    """Activate a fresh usage bucket for the current cron execution context."""
    usage = CronRunUsage()
    token = _ACTIVE_CRON_USAGE.set(usage)
    try:
        yield usage
    finally:
        _ACTIVE_CRON_USAGE.reset(token)


def record_native_usage(
    usage: CanonicalUsage | None,
    provider: str,
    model: str,
    base_url: str,
) -> bool:
    """Record a request iff it belongs to the active cron execution."""
    active = _ACTIVE_CRON_USAGE.get()
    if active is None:
        return False
    active.record(usage, provider, model, base_url)
    return True
