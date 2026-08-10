"""Read-only fleet reporting.

Reporting must never create, migrate, checkpoint, or otherwise write the
activity database, and must never relabel process-level completion as
semantic success.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from activity_telemetry.report import summarize
from activity_telemetry.schema import (
    LogicalActivityStart,
    OutcomeLayers,
    RouteUsageDelta,
    ServedRoute,
)
from activity_telemetry.store import ActivityStore

BASE = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
SINCE = "2026-08-01T00:00:00+00:00"


def _clock(*offsets):
    times = iter([BASE + timedelta(seconds=offset) for offset in offsets])
    return lambda: next(times)


def _seed(path):
    """Two runs of activity ``x``: v1 unknown on deepseek, v2 succeeded on codex."""
    store = ActivityStore(path, clock=_clock(0, 2))
    store.start(LogicalActivityStart(
        run_id="run-1", correlation_id="run-1", activity_id="x", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
        requested_provider="deepseek", requested_model="deepseek-v4-pro",
    ))
    store.record_usage("run-1", ServedRoute("deepseek", "deepseek-v4-pro"), RouteUsageDelta(
        turns=1, model_calls=1, tool_calls=2, retries=0,
        uncached_input_tokens=10, cache_read_tokens=90, cache_write_tokens=5,
        output_tokens=7, reasoning_tokens=3,
        recorded_provider_cost_usd=Decimal("0.12"),
        api_equivalent_cost_usd=Decimal("0.80"),
    ))
    store.finish("run-1", OutcomeLayers(process="succeeded"))

    store = ActivityStore(path, clock=_clock(10, 14))
    store.start(LogicalActivityStart(
        run_id="run-2", correlation_id="run-2", activity_id="x", policy_version=2,
        trigger_source="cron", profile="main", effective_hermes_home="H",
        requested_provider="deepseek", requested_model="deepseek-v4-pro",
    ))
    store.record_usage("run-2", ServedRoute("openai-codex", "gpt-5.6-sol"),
                       RouteUsageDelta(turns=1, model_calls=1))
    store.finish("run-2", OutcomeLayers(
        process="succeeded", protocol="succeeded", artifact="succeeded",
        domain="succeeded", delivery="succeeded",
    ))
    return path


@pytest.fixture
def seed_activity_db(tmp_path):
    return _seed(tmp_path / "activity.db")


def _keyed(rows):
    return {
        (r["activity_id"], r["policy_version"], r["served_model"], r["final_outcome"]): r
        for r in rows
    }


def test_summary_keeps_unknown_success_and_routes_separate(seed_activity_db):
    keyed = _keyed(summarize(seed_activity_db, since=SINCE))
    unknown = keyed[("x", 1, "deepseek-v4-pro", "unknown")]
    succeeded = keyed[("x", 2, "gpt-5.6-sol", "succeeded")]

    assert unknown["runs"] == 1
    assert succeeded["runs"] == 1
    assert unknown["cache_read_tokens"] == 90
    assert unknown["semantic_successes"] == 0
    assert unknown["unknowns"] == 1
    assert succeeded["semantic_successes"] == 1
    assert succeeded["unknowns"] == 0


def test_requested_route_is_reported_separately_from_served(seed_activity_db):
    keyed = _keyed(summarize(seed_activity_db, since=SINCE))
    switched = keyed[("x", 2, "gpt-5.6-sol", "succeeded")]

    assert switched["requested_provider"] == "deepseek"
    assert switched["requested_model"] == "deepseek-v4-pro"
    assert switched["served_provider"] == "openai-codex"


def test_costs_round_trip_as_exact_strings_and_zero_differs_from_unknown(seed_activity_db):
    keyed = _keyed(summarize(seed_activity_db, since=SINCE))

    assert keyed[("x", 1, "deepseek-v4-pro", "unknown")]["recorded_provider_cost_usd"] == "0.12"
    assert keyed[("x", 1, "deepseek-v4-pro", "unknown")]["api_equivalent_cost_usd"] == "0.80"
    # run-2 recorded no cost at all — that is unknown, not zero.
    assert keyed[("x", 2, "gpt-5.6-sol", "succeeded")]["recorded_provider_cost_usd"] is None


def test_zero_cost_is_preserved_as_zero(tmp_path):
    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=_clock(0, 1))
    store.start(LogicalActivityStart(
        run_id="r", correlation_id="r", activity_id="free", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))
    store.record_usage("r", ServedRoute("local", "llama"), RouteUsageDelta(
        model_calls=1, recorded_provider_cost_usd=Decimal("0"),
    ))
    store.finish("r", OutcomeLayers(process="succeeded"))

    row = summarize(path, since=SINCE)[0]
    assert row["recorded_provider_cost_usd"] == "0"
    assert row["api_equivalent_cost_usd"] is None


def test_runs_count_distinct_runs_across_multiple_served_routes(tmp_path):
    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=_clock(0, 3))
    store.start(LogicalActivityStart(
        run_id="r", correlation_id="r", activity_id="multi", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))
    for provider, model in (("a", "m1"), ("b", "m2")):
        store.record_usage("r", ServedRoute(provider, model), RouteUsageDelta(model_calls=1))
    store.finish("r", OutcomeLayers(process="succeeded"))

    rows = summarize(path, since=SINCE)
    assert len(rows) == 2
    assert {r["served_model"] for r in rows} == {"m1", "m2"}
    assert [r["runs"] for r in rows] == [1, 1]
    assert sum(r["model_calls"] for r in rows) == 2


def test_runs_without_usage_still_appear(tmp_path):
    """Deterministic and no-work fires never record a served route."""
    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=_clock(0, 1))
    store.start(LogicalActivityStart(
        run_id="r", correlation_id="r", activity_id="d0", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))
    store.finish("r", OutcomeLayers(process="no_work"))

    row = summarize(path, since=SINCE)[0]
    assert row["served_provider"] is None
    assert row["served_model"] is None
    assert row["final_outcome"] == "no_work"
    assert row["runs"] == 1
    assert row["model_calls"] == 0


def test_average_wall_time_and_null_wall_time(tmp_path):
    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=_clock(0, 4, 10))
    store.start(LogicalActivityStart(
        run_id="done", correlation_id="done", activity_id="w", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))
    store.finish("done", OutcomeLayers(process="succeeded"))
    store.start(LogicalActivityStart(
        run_id="open", correlation_id="open", activity_id="w", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))

    rows = summarize(path, since=SINCE)
    assert [r["final_outcome"] for r in rows] == ["unknown"]
    assert rows[0]["runs"] == 2
    # Only the finished run has a wall time; the open run must not skew it.
    assert rows[0]["average_wall_time_ms"] == 4000


def test_since_bound_excludes_older_runs(seed_activity_db):
    later = (BASE + timedelta(seconds=5)).isoformat()
    rows = summarize(seed_activity_db, since=later)

    assert [r["policy_version"] for r in rows] == [2]


def test_missing_database_raises_without_creating_it(tmp_path):
    missing = tmp_path / "nope" / "activity.db"
    with pytest.raises(FileNotFoundError):
        summarize(missing, since=SINCE)
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.parametrize("since", ["not-a-date", "2026-08-01T00:00:00", "", None, 20260801])
def test_malformed_or_naive_since_is_rejected(seed_activity_db, since):
    with pytest.raises(ValueError, match="since"):
        summarize(seed_activity_db, since=since)


def test_non_utc_since_is_normalized(seed_activity_db):
    # 09:00-04:00 is 13:00Z, after run-1 (12:00Z) and before run-2 (12:00:10Z)
    # is excluded too, so nothing remains.
    assert summarize(seed_activity_db, since="2026-08-10T09:00:00-04:00") == []
    # 07:00-04:00 is 11:00Z, before both runs.
    assert len(summarize(seed_activity_db, since="2026-08-10T07:00:00-04:00")) == 2


def test_windows_path_with_spaces_and_hash_is_supported(tmp_path):
    directory = tmp_path / "od d dir #1"
    directory.mkdir()
    path = _seed(directory / "activity db.sqlite")

    assert len(summarize(path, since=SINCE)) == 2


def test_reporting_never_writes_the_database(seed_activity_db):
    before = seed_activity_db.read_bytes()
    before_stat = seed_activity_db.stat().st_mtime_ns
    sidecars_before = sorted(p.name for p in seed_activity_db.parent.iterdir())

    summarize(seed_activity_db, since=SINCE)

    assert seed_activity_db.read_bytes() == before
    assert seed_activity_db.stat().st_mtime_ns == before_stat
    assert sorted(p.name for p in seed_activity_db.parent.iterdir()) == sidecars_before


def test_read_only_connection_rejects_writes(seed_activity_db, monkeypatch):
    """The connection itself must be read-only, not merely unused for writes."""
    captured = {}
    real_connect = sqlite3.connect

    def _capture(database, *args, **kwargs):
        captured["uri"] = database
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _capture)
    summarize(seed_activity_db, since=SINCE)

    assert "mode=ro" in captured["uri"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        real_connect(captured["uri"], uri=True).execute(
            "UPDATE logical_activity_runs SET final_outcome = 'succeeded'"
        )


def test_report_reads_one_consistent_snapshot(tmp_path, monkeypatch):
    """Counters and exact costs must describe the same instant.

    Reporting runs against the live, actively-written database. If the
    aggregate query and the exact-cost query see different snapshots, a row can
    report counters from before a write and costs from after it — an
    internally impossible result that silently corrupts cost-per-outcome.
    """
    from activity_telemetry import report as report_module

    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=lambda: BASE)
    store.start(LogicalActivityStart(
        run_id="r", correlation_id="r", activity_id="live", policy_version=1,
        trigger_source="cron", profile="main", effective_hermes_home="H",
    ))
    route = ServedRoute("p", "m")
    store.record_usage("r", route, RouteUsageDelta(
        turns=1, model_calls=1, recorded_provider_cost_usd=Decimal("0.10"),
    ))

    real_connection = report_module._read_only_connection

    class InterleavingConnection:
        """Commits a concurrent write just before the exact-cost query runs."""

        def __init__(self, inner):
            self._inner = inner
            self._wrote = False

        def execute(self, sql, *args, **kwargs):
            if "recorded_provider_cost_usd, u.api_equivalent_cost_usd" in sql and not self._wrote:
                self._wrote = True
                store.record_usage("r", route, RouteUsageDelta(
                    turns=5, model_calls=5, recorded_provider_cost_usd=Decimal("0.50"),
                ))
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(
        report_module, "_read_only_connection",
        lambda db: InterleavingConnection(real_connection(db)),
    )

    row = summarize(path, since=SINCE)[0]

    # Either both halves see the pre-write state or both see the post-write
    # state. A mix is the bug.
    assert (row["model_calls"], row["recorded_provider_cost_usd"]) in {
        (1, "0.10"), (6, "0.60"),
    }, f"torn read: model_calls={row['model_calls']} cost={row['recorded_provider_cost_usd']}"


def test_rows_expose_exactly_the_documented_columns(seed_activity_db):
    expected = {
        "activity_id", "policy_version", "requested_provider", "requested_model",
        "served_provider", "served_model", "final_outcome", "runs",
        "semantic_successes", "unknowns", "turns", "model_calls", "tool_calls",
        "retries", "uncached_input_tokens", "cache_read_tokens",
        "cache_write_tokens", "output_tokens", "reasoning_tokens",
        "recorded_provider_cost_usd", "api_equivalent_cost_usd",
        "average_wall_time_ms",
    }
    for row in summarize(seed_activity_db, since=SINCE):
        assert set(row) == expected
