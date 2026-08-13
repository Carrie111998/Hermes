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


def test_load_normalizes_malformed_yaml_to_policy_error(tmp_path):
    path = tmp_path / "malformed.yaml"
    path.write_text("enforcement: observe\nactivities: [\n", encoding="utf-8")

    with pytest.raises(PolicyError, match="invalid policy YAML") as raised:
        ActivityRegistry.load(path)

    assert raised.value.__cause__ is not None
    assert raised.value.__cause__.__class__.__module__.startswith("yaml")


def test_load_normalizes_unhashable_mapping_key_to_policy_error(tmp_path):
    path = tmp_path / "unhashable-key.yaml"
    path.write_text(
        "enforcement: observe\nactivities:\n  ? [bad, key]\n  : {}\n",
        encoding="utf-8",
    )

    with pytest.raises(PolicyError, match="invalid policy YAML") as raised:
        ActivityRegistry.load(path)

    assert isinstance(raised.value.__cause__, TypeError)
    assert "unhashable type" in str(raised.value.__cause__)


# ---------------------------------------------------------------------------
# Classification quality
# ---------------------------------------------------------------------------
#
# A registry where every activity carries the same class is a labelling
# exercise, not a policy. These tests assert the packaged file actually
# expresses the D0..P4 differentiation the design is built on, and that it
# covers the whole enabled fleet rather than only the model-capable half.


def _fixture(name):
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_enabled_deterministic_job_has_a_d0_policy():
    registry = ActivityRegistry.load_default()
    missing = []
    wrong_class = []
    for row in _fixture("enabled_deterministic_jobs.json"):
        policy = registry.resolve(alias=row["name"])
        if policy is None:
            missing.append(row["name"])
        elif policy.execution_class != "D0":
            wrong_class.append((row["name"], policy.execution_class))
    assert missing == [], f"deterministic jobs with no policy: {missing}"
    assert wrong_class == [], f"deterministic jobs not classified D0: {wrong_class}"


def test_every_enabled_model_capable_job_is_not_d0():
    registry = ActivityRegistry.load_default()
    wrong = []
    for row in _fixture("enabled_model_capable_jobs.json"):
        policy = registry.resolve(alias=row["name"])
        assert policy is not None, f"model-capable job unmapped: {row['name']}"
        if policy.execution_class == "D0":
            wrong.append(row["name"])
    assert wrong == [], f"model-capable jobs classified D0: {wrong}"


def test_classification_is_not_degenerate():
    """Guards the exact defect this file shipped with: everything one class."""
    registry = ActivityRegistry.load_default()
    classes = {p.execution_class for p in registry.policies.values()}
    assert "D0" in classes, "no deterministic activities declared"
    assert len(classes) >= 3, f"classification collapsed into {sorted(classes)}"


def test_reasoning_effort_rises_with_class():
    """Effort is the differentiator we can justify without evaluation evidence."""
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    ceiling = {"D0": 0, "S1": 1, "S2": 2, "P3": 3, "P4": 3}
    registry = ActivityRegistry.load_default()
    for policy in registry.policies.values():
        effort = order.get(policy.reasoning_effort)
        assert effort is not None, f"{policy.activity_id}: odd effort {policy.reasoning_effort!r}"
        assert effort <= ceiling[policy.execution_class], (
            f"{policy.activity_id} ({policy.execution_class}) has effort "
            f"{policy.reasoning_effort}"
        )


def test_budgets_scale_with_class():
    registry = ActivityRegistry.load_default()
    by_class = {}
    for policy in registry.policies.values():
        by_class.setdefault(policy.execution_class, []).append(policy.budgets.max_model_calls)
    for cls, calls in by_class.items():
        if cls == "D0":
            assert set(calls) == {0}, "D0 must never budget a model call"
    if "S1" in by_class and "P3" in by_class:
        assert max(by_class["S1"]) < max(by_class["P3"]), (
            "bounded work must not be budgeted like critical generation"
        )


class TestPremiumQualityFloors:
    """Premium-routing Task 1, written against activity IDs that exist.

    The plan asserts on `jobflow.researcher`, `jobflow.matcher.semantic`,
    `jobflow.tailor.strategy` and `jobflow.application_qc` — four IDs that are
    not in the registry — and on an `allowed_routes[].tier` field the schema
    does not have. Corrected here to the real IDs and the real fields, so the
    intent (a premium workload can never silently resolve to an economy
    policy) is actually enforced.
    """

    PREMIUM = (
        "cron.jobflow.matcher",
        "cron.jobflow.researcher",
        "cron.jobflow.applier",
        "jobflow.tailor.generate",
    )

    def test_premium_jobflow_activities_are_p3_with_a_premium_floor(self):
        from activity_policy.registry import ActivityRegistry

        registry = ActivityRegistry.load_default()
        for activity_id in self.PREMIUM:
            policy = registry.policies[activity_id]
            assert policy.execution_class in {"P3", "P4"}, activity_id
            assert policy.quality_floor == "premium", activity_id

    def test_every_premium_floor_activity_is_a_premium_class(self):
        """The two fields must not drift apart in either direction."""
        from activity_policy.registry import ActivityRegistry

        registry = ActivityRegistry.load_default()
        for activity_id, policy in registry.policies.items():
            if policy.quality_floor == "premium":
                assert policy.execution_class in {"P3", "P4"}, activity_id
            if policy.execution_class in {"P3", "P4"}:
                assert policy.quality_floor == "premium", activity_id

    def test_premium_activities_do_not_reason_at_the_lowest_effort(self):
        from activity_policy.registry import ActivityRegistry

        registry = ActivityRegistry.load_default()
        for activity_id in self.PREMIUM:
            assert registry.policies[activity_id].reasoning_effort in {"high", "xhigh"}, activity_id


class TestRetiredActivitiesAreRemoved:
    """A policy for a cron that no longer runs is stale weight, not history.

    The matcher shadow programme was retired 2026-08-13 — both crons disabled,
    graphs/jobflow.py left with no scheduled consumer. Its telemetry rows
    remain (the store is append-only and that record is worth keeping), but the
    registry describes what the fleet DOES, and it no longer does this.
    """

    @pytest.mark.parametrize("retired", (
        "cron.jobflow.matcher.shadow",
        "cron.jobflow.matcher.shadow.diff",
    ))
    def test_the_retired_shadow_activities_have_no_policy(self, retired):
        from activity_policy.registry import ActivityRegistry

        registry = ActivityRegistry.load_default()
        assert retired not in registry.policies

    @pytest.mark.parametrize("alias", (
        "jobflow-matcher-shadow",
        "jobflow-matcher-shadow-diff",
    ))
    def test_the_retired_shadow_aliases_resolve_to_nothing(self, alias):
        from activity_policy.registry import ActivityRegistry

        registry = ActivityRegistry.load_default()
        assert registry.resolve(alias=alias) is None
