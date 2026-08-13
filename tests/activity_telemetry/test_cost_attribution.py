"""Cost attribution at the recorder seam.

Without this, every usage row carries NULL in both cost columns — which the
schema deliberately reads as "unknown", not "free". A fleet whose cost is
100% unknown cannot answer the one question the telemetry exists to answer,
and Phase 4's release gate ("cost is a tie-breaker after quality floors")
and Phase 7 ("compare cost per semantic success") both depend on it.

Two columns, not one, because a subscription route bills nothing at the
margin while the same tokens still have a published API price. Collapsing
them makes a subscription workload incomparable with a metered one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from activity_telemetry.recorder import ActivityRecorder
from activity_telemetry.store import ActivityStore


def _open(tmp_path, provider="deepseek", model="deepseek-v4-pro"):
    return ActivityRecorder.open(
        tmp_path / "activity.db",
        run_id="r1",
        correlation_id="r1",
        activity_id="cron.jobflow.matcher",
        policy_version=2,
        trigger_source="test",
        profile="main",
        effective_hermes_home=str(tmp_path),
        requested_provider=provider,
        requested_model=model,
    )


def _usage(**over):
    base = {
        "input_tokens": 1_000_000,
        "cache_read_tokens": 2_000_000,
        "cache_write_tokens": 0,
        "output_tokens": 100_000,
        "reasoning_tokens": 50_000,
        "request_count": 1,
    }
    base.update(over)
    return base


def _row(tmp_path):
    store = ActivityStore(tmp_path / "activity.db")
    conn = store._get_conn()
    return conn.execute(
        "SELECT * FROM logical_activity_route_usage WHERE run_id = 'r1'"
    ).fetchone()


class TestMeteredRoute:
    def test_records_both_costs_for_a_priced_model(self, tmp_path):
        rec = _open(tmp_path)
        rec.record_response("deepseek", "deepseek-v4-pro", _usage())

        row = _row(tmp_path)
        assert row["recorded_provider_cost_usd"] is not None, (
            "a priced metered route must not record cost as unknown"
        )
        assert Decimal(row["recorded_provider_cost_usd"]) > 0

    def test_metered_recorded_cost_equals_api_equivalent(self, tmp_path):
        """No subscription in play, so the two views must agree exactly."""
        rec = _open(tmp_path)
        rec.record_response("deepseek", "deepseek-v4-pro", _usage())

        row = _row(tmp_path)
        assert Decimal(row["recorded_provider_cost_usd"]) == Decimal(
            row["api_equivalent_cost_usd"]
        )

    def test_cache_reads_are_priced_below_uncached_input(self, tmp_path):
        """The cache economics this fleet runs on must show up in the number.

        96% of fleet input is cache reads. If cache reads were billed at the
        uncached rate the totals would be wildly overstated.
        """
        cheap = _open(tmp_path)
        cheap.record_response(
            "deepseek", "deepseek-v4-pro",
            _usage(input_tokens=0, cache_read_tokens=3_000_000),
        )
        cheap_cost = Decimal(_row(tmp_path)["recorded_provider_cost_usd"])

        (tmp_path / "b").mkdir()
        dear = _open(tmp_path / "b")
        dear.record_response(
            "deepseek", "deepseek-v4-pro",
            _usage(input_tokens=3_000_000, cache_read_tokens=0),
        )
        dear_cost = Decimal(_row(tmp_path / "b")["recorded_provider_cost_usd"])

        assert cheap_cost < dear_cost


class TestSubscriptionRoute:
    """A Codex-OAuth call costs nothing at the margin but is not free to compare."""

    def test_recorded_cost_is_zero_not_unknown(self, tmp_path):
        rec = _open(tmp_path, provider="openai-codex", model="gpt-5.6-sol")
        rec.record_response("openai-codex", "gpt-5.6-sol", _usage())

        row = _row(tmp_path)
        assert row["recorded_provider_cost_usd"] is not None, (
            "subscription-included is KNOWN free (0), never unknown (NULL)"
        )
        assert Decimal(row["recorded_provider_cost_usd"]) == 0

    def test_api_equivalent_cost_is_the_published_rate(self, tmp_path):
        """Otherwise a subscription workload looks free and wins every comparison."""
        rec = _open(tmp_path, provider="openai-codex", model="gpt-5.6-sol")
        rec.record_response("openai-codex", "gpt-5.6-sol", _usage())

        row = _row(tmp_path)
        assert row["api_equivalent_cost_usd"] is not None
        assert Decimal(row["api_equivalent_cost_usd"]) > 0


class TestUnknownStaysUnknown:
    def test_unpriced_model_records_null_on_both_columns(self, tmp_path):
        rec = _open(tmp_path, provider="custom", model="no-such-model-xyz")
        rec.record_response("custom", "no-such-model-xyz", _usage())

        row = _row(tmp_path)
        assert row["recorded_provider_cost_usd"] is None, (
            "no pricing means unknown (NULL); 0 would claim it was free"
        )
        assert row["api_equivalent_cost_usd"] is None


class TestPricingNeverBreaksTelemetry:
    def test_pricing_failure_still_records_the_usage_row(self, tmp_path, monkeypatch):
        """Cost is an enrichment. Losing it must not lose the token counts."""
        import activity_telemetry.recorder as mod

        def _boom(*a, **k):
            raise RuntimeError("pricing table unavailable")

        monkeypatch.setattr(mod, "estimate_usage_cost", _boom, raising=False)

        rec = _open(tmp_path)
        rec.record_response("deepseek", "deepseek-v4-pro", _usage())

        row = _row(tmp_path)
        assert row is not None, "usage row must survive a pricing failure"
        assert row["uncached_input_tokens"] == 1_000_000
        assert row["recorded_provider_cost_usd"] is None

    def test_pricing_failure_does_not_raise(self, tmp_path, monkeypatch):
        import activity_telemetry.recorder as mod

        monkeypatch.setattr(
            mod, "estimate_usage_cost",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
            raising=False,
        )
        rec = _open(tmp_path)
        rec.record_response("deepseek", "deepseek-v4-pro", _usage())  # must not raise
