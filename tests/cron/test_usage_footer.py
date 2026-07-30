"""Behavior tests for native, per-cron-run usage footer aggregation."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from agent.usage_pricing import CanonicalUsage, CostResult


def _cost(amount):
    return CostResult(amount_usd=Decimal(amount), status="estimated", source="official_docs_snapshot", label="")


def test_footer_aggregates_native_usage_without_double_counting(monkeypatch):
    from cron.usage_footer import CronRunUsage

    monkeypatch.setattr("cron.usage_footer.estimate_usage_cost", lambda *a, **kw: _cost("0.0054"))
    usage = CronRunUsage()
    usage.record(
        CanonicalUsage(input_tokens=17700, output_tokens=312, reasoning_tokens=2400,
                       cache_read_tokens=8100, cache_write_tokens=20),
        provider="openai", model="gpt-5.6-terra", base_url="https://api.openai.com/v1",
    )
    usage.record(CanonicalUsage(input_tokens=0, output_tokens=0), provider="openai", model="gpt-5.6-terra")

    footer = usage.footer()

    assert "Usage: 17.7k input · 312 output · 2.4k reasoning · 18.0k total" in footer
    assert "Cache: 8.1k read · 20 write" in footer
    assert "Estimated cost: $0.0108 · 2 LLM calls" in footer
    # Reasoning/cache are provider sub-buckets: displayed total is input + output only.
    assert "28.5k total" not in footer


def test_footer_marks_partially_known_cost_incomplete(monkeypatch):
    from cron.usage_footer import CronRunUsage

    estimates = iter([_cost("0.0003"), CostResult(None, "unknown", "none", "n/a")])
    monkeypatch.setattr("cron.usage_footer.estimate_usage_cost", lambda *a, **kw: next(estimates))
    usage = CronRunUsage()
    usage.record(CanonicalUsage(input_tokens=842, output_tokens=91), provider="openai", model="known")
    usage.record(CanonicalUsage(input_tokens=10, output_tokens=2), provider="unknown", model="unknown")

    assert "Estimated cost incomplete: $0.0003 · 2 LLM calls" in usage.footer()


def test_footer_handles_unavailable_cost_and_multiple_routes(monkeypatch):
    from cron.usage_footer import CronRunUsage

    monkeypatch.setattr(
        "cron.usage_footer.estimate_usage_cost",
        lambda *a, **kw: CostResult(None, "unknown", "none", "n/a"),
    )
    usage = CronRunUsage()
    usage.record(CanonicalUsage(input_tokens=21_300, output_tokens=608), provider="openai", model="a")
    usage.record(CanonicalUsage(), provider="anthropic", model="b")

    footer = usage.footer()
    assert "Usage: 21.3k input · 608 output · 21.9k total" in footer
    assert "Estimated cost unavailable · 2 LLM calls · Routes: 2" in footer


def test_context_scopes_concurrent_runs_and_ignores_interactive_calls(monkeypatch):
    from cron.usage_footer import activate_cron_usage, record_native_usage

    monkeypatch.setattr("cron.usage_footer.estimate_usage_cost", lambda *a, **kw: _cost("0"))
    # No active cron scope: interactive usage remains unaffected.
    assert record_native_usage(CanonicalUsage(input_tokens=999, output_tokens=1), "p", "interactive", "") is False

    def one_run(tokens):
        with activate_cron_usage() as usage:
            assert record_native_usage(CanonicalUsage(input_tokens=tokens, output_tokens=1), "p", "m", "") is True
            return usage.footer()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(one_run, (100, 200)))

    assert "100 input · 1 output · 101 total" in first
    assert "200 input · 1 output · 201 total" in second


def test_footer_is_empty_when_no_native_usage():
    from cron.usage_footer import CronRunUsage

    assert CronRunUsage().footer() == ""
