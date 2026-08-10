from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3

import pytest

from activity_telemetry.schema import LogicalActivityStart, OutcomeLayers, RouteUsageDelta, ServedRoute
from activity_telemetry.store import ActivityStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _start(run_id="run-1", **overrides):
    values = dict(run_id=run_id, correlation_id="corr-1", activity_id="jobflow.tailor.generate",
                  policy_version=1, trigger_source="cron", profile="tailor",
                  effective_hermes_home="X", requested_provider="deepseek",
                  requested_model="deepseek-v4-pro", session_id=None, parent_run_id=None,
                  child_depth=0)
    values.update(overrides)
    return LogicalActivityStart(**values)


def test_start_usage_link_and_single_terminal_enrichment(tmp_path):
    times = iter((NOW, NOW + timedelta(seconds=2)))
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: next(times))
    store.start(_start())
    store.link_session("run-1", "cron_a_20260810_120000")
    route = ServedRoute("openai-codex", "gpt-5.6-sol")
    store.record_usage("run-1", route, RouteUsageDelta(
        turns=1, model_calls=1, tool_calls=2, retries=0,
        uncached_input_tokens=10, cache_read_tokens=90, cache_write_tokens=5,
        output_tokens=7, reasoning_tokens=3,
        recorded_provider_cost_usd=Decimal("0.12"), api_equivalent_cost_usd=Decimal("0.80")))
    final = store.finish("run-1", OutcomeLayers(process="succeeded"),
                         ("session:cron_a_20260810_120000",))
    assert final == "unknown"
    row = store.get_run("run-1")
    assert row["session_id"] == "cron_a_20260810_120000"
    assert row["final_outcome"] == "unknown"
    assert row["delivery_outcome"] == "unknown"
    assert row["wall_time_ms"] == 2000
    assert json.loads(row["evidence_refs_json"]) == ["session:cron_a_20260810_120000"]
    usage = store.get_routes("run-1")[0]
    assert usage["cache_read_tokens"] == 90
    assert usage["recorded_provider_cost_usd"] == Decimal("0.12")
    with pytest.raises(ValueError, match="already finished"):
        store.finish("run-1", OutcomeLayers(process="failed"))


def test_model_switches_have_distinct_route_rows(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    for provider, model in (("deepseek", "deepseek-v4-pro"), ("openai-codex", "gpt-5.6-sol")):
        store.record_usage("run-1", ServedRoute(provider, model), RouteUsageDelta(model_calls=1))
    assert [(r["served_provider"], r["served_model"]) for r in store.get_routes("run-1")] == [
        ("deepseek", "deepseek-v4-pro"), ("openai-codex", "gpt-5.6-sol")]


def test_duplicate_start_unknown_run_and_single_session_link(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    with pytest.raises(ValueError, match="already exists"):
        store.start(_start())
    store.link_session("run-1", "s1")
    with pytest.raises(ValueError, match="already linked"):
        store.link_session("run-1", "s2")
    with pytest.raises(KeyError, match="missing"):
        store.link_session("missing", "s")
    with pytest.raises(KeyError, match="missing"):
        store.record_usage("missing", ServedRoute("p", "m"), RouteUsageDelta())
    with pytest.raises(KeyError, match="missing"):
        store.finish("missing", OutcomeLayers(process="failed"))


def test_session_id_is_unique_between_runs(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start("run-1"))
    store.start(_start("run-2"))
    store.link_session("run-1", "shared")
    with pytest.raises(ValueError, match="session_id"):
        store.link_session("run-2", "shared")
    assert store.get_run("run-2")["session_id"] is None


def test_usage_and_session_cannot_change_finished_run(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    store.finish("run-1", OutcomeLayers(process="no_work"))
    with pytest.raises(ValueError, match="already finished"):
        store.record_usage("run-1", ServedRoute("p", "m"), RouteUsageDelta(model_calls=1))
    with pytest.raises(ValueError, match="already finished"):
        store.link_session("run-1", "late")


def test_child_requires_existing_unfinished_parent_and_consistent_depth(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    with pytest.raises(ValueError, match="parent run does not exist"):
        store.start(_start(parent_run_id="missing", child_depth=1))
    store.start(_start(run_id="parent"))
    with pytest.raises(ValueError, match="child_depth"):
        store.start(_start(parent_run_id="parent", child_depth=2))
    store.finish("parent", OutcomeLayers(process="failed"))
    with pytest.raises(ValueError, match="parent run already finished"):
        store.start(_start(parent_run_id="parent", child_depth=1))
    assert store.get_run("parent")["child_count"] == 0


def test_usage_is_additive_and_cost_null_differs_from_zero(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    route = ServedRoute("p", "m")
    store.record_usage("run-1", route, RouteUsageDelta(model_calls=1, cache_read_tokens=2))
    store.record_usage("run-1", route, RouteUsageDelta(model_calls=3, cache_read_tokens=4,
        recorded_provider_cost_usd=Decimal("0"), api_equivalent_cost_usd=Decimal("1.25")))
    row = store.get_routes("run-1")[0]
    assert row["model_calls"] == 4
    assert row["cache_read_tokens"] == 6
    assert row["recorded_provider_cost_usd"] == Decimal("0")
    assert row["api_equivalent_cost_usd"] == Decimal("1.25")


def test_concurrent_usage_does_not_lose_updates(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    route = ServedRoute("p", "m")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.record_usage("run-1", route, RouteUsageDelta(model_calls=1)), range(40)))
    assert store.get_routes("run-1")[0]["model_calls"] == 40


def test_concurrent_store_instances_do_not_lose_decimal_cost(tmp_path):
    path = tmp_path / "activity.db"
    stores = [ActivityStore(path, clock=lambda: NOW) for _ in range(4)]
    stores[0].start(_start())
    route = ServedRoute("p", "m")
    delta = RouteUsageDelta(model_calls=1, recorded_provider_cost_usd=Decimal("0.10"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(stores[index % len(stores)].record_usage, "run-1", route, delta)
            for index in range(20)
        ]
        for future in futures:
            future.result()
    row = stores[0].get_routes("run-1")[0]
    assert row["model_calls"] == 20
    assert row["recorded_provider_cost_usd"] == Decimal("2.00")


def test_separate_store_cannot_record_usage_after_finish(tmp_path):
    path = tmp_path / "activity.db"
    usage_store = ActivityStore(path, clock=lambda: NOW)
    finish_store = ActivityStore(path, clock=lambda: NOW)
    usage_store.start(_start())
    finish_store.finish("run-1", OutcomeLayers(process="failed"))
    with pytest.raises(ValueError, match="already finished"):
        usage_store.record_usage(
            "run-1",
            ServedRoute("p", "m"),
            RouteUsageDelta(model_calls=1, recorded_provider_cost_usd=Decimal("0.10")),
        )
    assert usage_store.get_routes("run-1") == []


def test_escalation_and_evidence_are_validated(tmp_path):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    with pytest.raises(ValueError, match="escalation_reason"):
        store.finish("run-1", OutcomeLayers(process="blocked"), escalation_reason="surprise")
    with pytest.raises(ValueError, match="evidence"):
        store.finish("run-1", OutcomeLayers(process="failed"), evidence_refs=("",))


@pytest.mark.parametrize(
    "reference",
    (
        "https://example.test/result?token=secret",
        "artifact:{\"authorization\":\"Bearer secret\"}",
        "session:user:password@example.test",
        "note:key=value",
        "note:" + "x" * 500,
    ),
)
def test_evidence_rejects_payloads_urls_and_credential_shaped_values(tmp_path, reference):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    with pytest.raises(ValueError, match="evidence"):
        store.finish("run-1", OutcomeLayers(process="failed"), evidence_refs=(reference,))


def test_reopen_preserves_parent_child_relationship_and_terminal_data(tmp_path):
    path = tmp_path / "activity.db"
    store = ActivityStore(path, clock=lambda: NOW)
    store.start(_start(run_id="parent"))
    store.start(_start(parent_run_id="parent", child_depth=1))
    store.finish("run-1", OutcomeLayers(process="no_work"))
    reopened = ActivityStore(path, clock=lambda: NOW)
    row = reopened.get_run("run-1")
    assert row["parent_run_id"] == "parent"
    assert row["child_depth"] == 1
    assert row["final_outcome"] == "no_work"
    assert reopened.get_run("parent")["child_count"] == 1


def test_failed_write_rolls_back_and_connection_remains_usable(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "activity.db", clock=lambda: NOW)
    store.start(_start())
    original = store._get_conn()

    class FailingConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("injected")
        def rollback(self):
            original.rollback()

    monkeypatch.setattr(store._local, "conn", FailingConnection())
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        store.record_usage("run-1", ServedRoute("p", "m"), RouteUsageDelta(model_calls=1))
    monkeypatch.setattr(store._local, "conn", original)
    store.record_usage("run-1", ServedRoute("p", "m"), RouteUsageDelta(model_calls=1))
    assert store.get_routes("run-1")[0]["model_calls"] == 1
