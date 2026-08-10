from decimal import Decimal

import pytest

from activity_telemetry.schema import (
    LogicalActivityStart,
    OutcomeLayers,
    RouteUsageDelta,
    ServedRoute,
    derive_final_outcome,
)


def test_missing_semantic_and_delivery_evidence_stays_unknown():
    assert derive_final_outcome(OutcomeLayers(process="succeeded")) == "unknown"


def test_terminal_precedence_and_no_work():
    assert derive_final_outcome(OutcomeLayers(process="no_work")) == "no_work"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="failed")) == "failed"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="budget_exhausted")) == "budget_exhausted"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="blocked")) == "blocked"
    assert derive_final_outcome(OutcomeLayers(process="succeeded", protocol="partial")) == "partial"


def test_success_requires_all_layers():
    layers = OutcomeLayers(**{name: "succeeded" for name in ("process", "protocol", "artifact", "domain", "delivery")})
    assert derive_final_outcome(layers) == "succeeded"


def test_invalid_layer_value_is_rejected():
    with pytest.raises(ValueError, match="outcome"):
        OutcomeLayers(process="green")


@pytest.mark.parametrize("field", ["run_id", "correlation_id", "activity_id", "trigger_source", "profile", "effective_hermes_home"])
def test_start_rejects_blank_identity(field):
    values = dict(run_id="r", correlation_id="c", activity_id="x", policy_version=1,
                  trigger_source="cron", profile="main", effective_hermes_home="X")
    values[field] = " "
    with pytest.raises(ValueError, match=field):
        LogicalActivityStart(**values)


def test_start_rejects_invalid_version_and_depth():
    with pytest.raises(ValueError, match="policy_version"):
        LogicalActivityStart("r", "c", "x", 0, "cron", "main", "X")
    with pytest.raises(ValueError, match="child_depth"):
        LogicalActivityStart("r", "c", "x", 1, "cron", "main", "X", child_depth=-1)


def test_route_and_usage_reject_invalid_values():
    with pytest.raises(ValueError, match="provider"):
        ServedRoute("", "m")
    with pytest.raises(ValueError, match="model"):
        ServedRoute("p", " ")
    with pytest.raises(ValueError, match="model_calls"):
        RouteUsageDelta(model_calls=-1)
    with pytest.raises(ValueError, match="recorded_provider_cost_usd"):
        RouteUsageDelta(recorded_provider_cost_usd=Decimal("-0.01"))


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_usage_rejects_non_integer_counters(value):
    with pytest.raises(ValueError, match="model_calls"):
        RouteUsageDelta(model_calls=value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_usage_rejects_non_finite_costs(value):
    with pytest.raises(ValueError, match="recorded_provider_cost_usd"):
        RouteUsageDelta(recorded_provider_cost_usd=value)


def test_start_rejects_blank_optional_identity():
    with pytest.raises(ValueError, match="parent_run_id"):
        LogicalActivityStart("r", "c", "x", 1, "cron", "main", "X", parent_run_id=" ")


def test_start_rejects_naive_clock(tmp_path):
    from datetime import datetime

    from activity_telemetry.store import ActivityStore

    store = ActivityStore(tmp_path / "activity.db", clock=lambda: datetime(2026, 8, 10))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.start(LogicalActivityStart("r", "c", "x", 1, "cron", "main", "X"))
