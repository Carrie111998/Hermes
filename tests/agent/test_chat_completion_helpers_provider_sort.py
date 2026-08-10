from types import SimpleNamespace

from agent.chat_completion_helpers import (
    _provider_preferences_for_agent,
    _validated_openrouter_performance_preference,
    _validated_openrouter_provider_sort,
)


def test_validated_openrouter_provider_sort_accepts_valid_values():
    assert _validated_openrouter_provider_sort("price") == "price"
    assert _validated_openrouter_provider_sort(" latency ") == "latency"
    assert _validated_openrouter_provider_sort("THROUGHPUT") == "throughput"


def test_validated_openrouter_provider_sort_rejects_invalid_values():
    assert _validated_openrouter_provider_sort("intelligence") is None
    assert _validated_openrouter_provider_sort("") is None
    assert _validated_openrouter_provider_sort(None) is None


def test_validated_openrouter_performance_preference_accepts_number_and_percentiles():
    assert _validated_openrouter_performance_preference(0.8, "preferred_max_latency") == 0.8
    assert _validated_openrouter_performance_preference(
        {"p50": 0.8, "p90": 3}, "preferred_max_latency"
    ) == {"p50": 0.8, "p90": 3.0}


def test_validated_openrouter_performance_preference_drops_invalid_entries():
    assert _validated_openrouter_performance_preference(True, "preferred_max_latency") is None
    assert _validated_openrouter_performance_preference(
        {"p50": -1, "p100": 2, "p90": 3}, "preferred_max_latency"
    ) == {"p90": 3.0}


def test_provider_preferences_include_performance_thresholds():
    agent = SimpleNamespace(
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort="latency",
        provider_require_parameters=False,
        provider_data_collection="allow",
        provider_preferred_min_throughput={"p50": 70},
        provider_preferred_max_latency={"p50": 0.8, "p90": 3},
    )
    assert _provider_preferences_for_agent(agent) == {
        "sort": "latency",
        "data_collection": "allow",
        "preferred_min_throughput": {"p50": 70.0},
        "preferred_max_latency": {"p50": 0.8, "p90": 3.0},
    }
