from __future__ import annotations

import pytest

from proactive.execution_backends import (
    ExecutionRequirements,
    assert_backend_transition,
    build_shadow_comparison_report,
    load_backend_registry,
    next_poll_delay_seconds,
    route_execution_backend,
    select_semantic_fallback,
)


def test_routes_isolated_readonly_browser_to_openclaw_with_audit_evidence():
    requirements = ExecutionRequirements.build(
        capabilities=[
            "browser_read",
            "isolated_session",
            "isolated_workspace",
        ],
        risk_level="low",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        max_runtime_seconds=300,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=123,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["decided_at"] == 123
    assert decision["mode"] == "shadow"
    assert decision["selection_reason"].endswith("openclaw")
    assert decision["candidates"][0] == {
        "backend": "openclaw",
        "eligible": True,
        "reasons": ["requirements_matched"],
        "cost_tier": "medium",
        "supports_async": True,
    }


def test_open_circuit_produces_deterministic_no_backend_decision():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read", "isolated_session"],
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        circuit_states={"openclaw": "open"},
        now=456,
    )

    assert decision["selected_backend"] is None
    assert decision["candidates"][0]["reasons"] == ["circuit_open"]
    assert all(not item["eligible"] for item in decision["candidates"])


def test_half_open_circuit_requires_an_explicit_single_probe():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read", "isolated_session"],
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        circuit_states={"openclaw": "half_open"},
        now=457,
    )

    assert decision["selected_backend"] is None
    assert decision["candidates"][0]["reasons"] == [
        "circuit_half_open_probe_required"
    ]


def test_preferred_backend_does_not_override_missing_capabilities():
    requirements = ExecutionRequirements.build(
        capabilities=["code", "tests"],
        preferred_backend="hermes",
        workspace_policy="dedicated",
        session_policy="managed",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=789,
    )

    assert decision["candidates"][0]["backend"] == "hermes"
    assert decision["candidates"][0]["eligible"] is False
    assert "missing_capabilities:code,tests" in decision["candidates"][0]["reasons"]
    assert decision["selected_backend"] is None
    codex = next(
        item for item in decision["candidates"] if item["backend"] == "codex"
    )
    assert "disabled" in codex["reasons"]


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, "queued"),
        ("queued", "running"),
        ("queued", "succeeded"),
        ("running", "running"),
        ("running", "failed"),
        ("succeeded", "succeeded"),
    ],
)
def test_backend_lifecycle_accepts_monotonic_transitions(previous, current):
    assert_backend_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("running", "queued"),
        ("succeeded", "running"),
        ("failed", "blocked"),
    ],
)
def test_backend_lifecycle_rejects_regression_or_terminal_rewrite(previous, current):
    with pytest.raises(ValueError):
        assert_backend_transition(previous, current)


def test_poll_backoff_is_bounded():
    assert [next_poll_delay_seconds(index) for index in range(6)] == [
        2,
        4,
        8,
        16,
        30,
        30,
    ]


def test_semantic_class_prevents_capability_only_fallback():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read"],
        semantic_class="browser_readonly",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=900,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["fallback_order"] == []
    assert select_semantic_fallback(
        decision,
        failed_backend="openclaw",
    ) is None
    assert decision["candidates"][1]["semantic_compatible"] is False
    assert "semantic_class_mismatch:browser_readonly" in decision["candidates"][1]["reasons"]


def test_semantic_fallback_selects_next_compatible_backend():
    registry = load_backend_registry()
    registry["backends"]["codex"]["enabled"] = True
    registry["backends"]["codex"]["semantic_classes"].append("analysis")
    requirements = ExecutionRequirements.build(
        capabilities=["analysis"],
        semantic_class="analysis",
    )

    decision = route_execution_backend(requirements, registry=registry, now=901)

    assert decision["selected_backend"] == "codex"
    assert select_semantic_fallback(
        decision,
        failed_backend="codex",
    ) == "hermes"


def test_shadow_report_compares_outcome_cost_duration_and_evidence():
    requirements = ExecutionRequirements.build(
        capabilities=["analysis"],
        semantic_class="analysis",
    )
    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=902,
    )

    report = build_shadow_comparison_report(
        decision,
        {
            "codex": {
                "status": "succeeded",
                "duration_ms": 1200,
                "cost_units": 3.5,
                "evidence_digest": "sha256:same",
            },
            "hermes": {
                "status": "succeeded",
                "duration_ms": 800,
                "cost_units": 1.5,
                "evidence_digest": "sha256:same",
            },
        },
    )

    assert report["selected_backend"] == "hermes"
    assert report["summary"] == {
        "observed_backends": 2,
        "comparable_backends": 1,
        "outcome_matches": 1,
        "evidence_matches": 1,
    }
    assert report["observations"][1]["duration_ms"] == 800


def test_shadow_report_requires_selected_backend_observation():
    requirements = ExecutionRequirements.build(capabilities=["analysis"])
    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=903,
    )

    with pytest.raises(
        ValueError,
        match="selected-backend observation",
    ):
        build_shadow_comparison_report(decision, {})
