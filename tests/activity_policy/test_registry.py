from importlib.resources import files
from pathlib import Path
import json

import pytest

from activity_policy.registry import ActivityRegistry
from activity_policy.schema import PolicyError


def _valid_policy(**overrides):
    data = {
        "policy_version": 1,
        "owner": "jobflow",
        "aliases": ["jobflow-tailor"],
        "execution_class": "P3",
        "quality_floor": "premium",
        "preferred_models": ["claude-opus-5"],
        "allowed_fallbacks": ["claude-sonnet-5"],
        "reasoning": {"mode": "adaptive", "effort": "high"},
        "budgets": {
            "max_turns": 30,
            "max_model_calls": 8,
            "max_uncached_input_tokens": 180000,
            "max_cache_read_tokens": 900000,
            "max_cache_write_tokens": 90000,
            "max_output_tokens": 30000,
            "max_reasoning_tokens": 60000,
            "max_tool_calls": 40,
            "wall_clock_seconds": 2400,
            "retries": 1,
            "max_children": 2,
            "max_child_depth": 1,
            "max_recorded_provider_cost_usd": "5.00",
            "max_api_equivalent_cost_usd": "25.00",
        },
        "tools": {"allow": ["read_file"], "deny": ["merge", "deploy"]},
        "outcome_contract": {
            "required_artifacts": ["tailoring-brief.md"],
            "required_validations": ["factuality"],
        },
        "escalation": {"on": ["quality_gate_failed"]},
    }
    data.update(overrides)
    return data


def _document(policy=None):
    return {"enforcement": "observe", "activities": {"jobflow.tailor.generate": policy or _valid_policy()}}


def test_default_registry_is_observational_and_packaged():
    assert files("activity_policy").joinpath("policies.yaml").is_file()
    registry = ActivityRegistry.load_default()
    assert registry.enforcement == "observe"
    assert registry.require("jobflow.tailor.generate").owner == "jobflow"
    assert registry.resolve(alias="jobflow-tailor").activity_id == "jobflow.tailor.generate"


def test_registry_rejects_unknown_keys_and_alias_collisions():
    policy = _valid_policy(aliases=["same"], surprise=True)
    with pytest.raises(PolicyError, match="unknown keys"):
        ActivityRegistry.from_mapping({"enforcement": "observe", "activities": {"x": policy}})

    with pytest.raises(PolicyError, match="duplicate alias"):
        ActivityRegistry.from_mapping({
            "enforcement": "observe",
            "activities": {
                "x": _valid_policy(aliases=["same"]),
                "y": _valid_policy(aliases=["same"]),
            },
        })


def test_default_aliases_cover_frozen_model_capable_inventory():
    fixture = Path(__file__).parent / "fixtures" / "enabled_model_capable_jobs.json"
    jobs = json.loads(fixture.read_text(encoding="utf-8"))
    registry = ActivityRegistry.load_default()
    missing = sorted(job["name"] for job in jobs if registry.resolve(alias=job["name"]) is None)
    assert missing == []


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"execution_class": "P9"}, "execution class"),
        ({"policy_version": 0}, "policy_version"),
        ({"policy_version": -1}, "policy_version"),
        ({"owner": " "}, "owner"),
        ({"quality_floor": "best"}, "quality floor"),
        ({"reasoning": {"mode": "adaptive"}}, "reasoning"),
        ({"reasoning": {"mode": "adaptive", "effort": "high", "extra": True}}, "unknown keys"),
        ({"tools": {"allow": ["read_file"], "deny": ["read_file"]}}, "overlap"),
        ({"tools": {"allow": [], "deny": []}}, "allowlist"),
        ({"escalation": {"on": ["invented"]}}, "escalation"),
    ],
)
def test_policy_rejects_invalid_declarations(overrides, match):
    with pytest.raises(PolicyError, match=match):
        ActivityRegistry.from_mapping(_document(_valid_policy(**overrides)))


@pytest.mark.parametrize("bad_value", [-1, 1.5, True, "1"])
def test_policy_rejects_negative_or_non_integer_budgets(bad_value):
    budgets = _valid_policy()["budgets"] | {"max_turns": bad_value}
    with pytest.raises(PolicyError, match="max_turns"):
        ActivityRegistry.from_mapping(_document(_valid_policy(budgets=budgets)))


def test_policy_rejects_missing_budget():
    budgets = dict(_valid_policy()["budgets"])
    budgets.pop("max_turns")
    with pytest.raises(PolicyError, match="budgets"):
        ActivityRegistry.from_mapping(_document(_valid_policy(budgets=budgets)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"quality_floor": "standard"},
        {"preferred_models": ["model"]},
        {"budgets": _valid_policy()["budgets"] | {"max_model_calls": 1}},
    ],
)
def test_d0_rejects_model_routes_positive_calls_or_wrong_floor(overrides):
    base = {
        "execution_class": "D0",
        "quality_floor": "deterministic",
        "preferred_models": [],
        "allowed_fallbacks": [],
        "tools": {"allow": [], "deny": []},
        "budgets": _valid_policy()["budgets"] | {"max_model_calls": 0},
    }
    base.update(overrides)
    with pytest.raises(PolicyError, match="D0"):
        ActivityRegistry.from_mapping(_document(_valid_policy(**base)))


def test_registry_rejects_invalid_root_and_activity_ids():
    with pytest.raises(PolicyError, match="unknown keys"):
        ActivityRegistry.from_mapping(_document() | {"extra": True})
    with pytest.raises(PolicyError, match="enforcement"):
        ActivityRegistry.from_mapping(_document() | {"enforcement": "enforce"})
    with pytest.raises(PolicyError, match="activity ID"):
        ActivityRegistry.from_mapping({"enforcement": "observe", "activities": {"not-dotted": _valid_policy()}})


def test_registry_rejects_alias_that_collides_with_activity_id():
    with pytest.raises(PolicyError, match="collides with activity ID"):
        ActivityRegistry.from_mapping({
            "enforcement": "observe",
            "activities": {
                "one.id": _valid_policy(aliases=["two.id"]),
                "two.id": _valid_policy(aliases=[]),
            },
        })


def test_resolve_preserves_observational_legacy_behavior():
    registry = ActivityRegistry.from_mapping(_document())
    assert registry.resolve(alias="legacy-unmapped") is None
    with pytest.raises(PolicyError, match="not found"):
        registry.resolve(activity_id="missing.id")


def test_load_rejects_duplicate_activity_ids_after_yaml_construction(tmp_path):
    policy = _valid_policy(aliases=[])
    declaration = json.dumps(policy)
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        "enforcement: observe\nactivities:\n"
        f"  duplicate.id: {declaration}\n"
        f"  duplicate.id: {declaration}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError, match="duplicate activity ID"):
        ActivityRegistry.load(path)
