"""Focused contracts for the fail-closed Kanban model router."""

import json

import pytest

from agent.task_model_router import (
    BASE_PROVIDER,
    LUNA_MODEL,
    ROUTE_VERSION,
    SOL_MODEL,
    TERRA_MODEL,
    route_task_model,
    validate_routing_metadata,
)


def _metadata(**overrides):
    metadata = {
        "enabled": True,
        "provider": BASE_PROVIDER,
        "model": LUNA_MODEL,
        "risk_level": "low",
    }
    metadata.update(overrides)
    return metadata


@pytest.mark.parametrize("metadata", [None, {}, [], "metadata"])
def test_missing_or_non_mapping_metadata_bypasses_without_route(metadata):
    missing = route_task_model(metadata)

    assert missing.bypass is True
    assert missing.routed is False
    assert missing.selected_model is None
    assert missing.reason_codes == ("missing_metadata",)


def test_disabled_metadata_bypasses_without_route():
    disabled = route_task_model(_metadata(enabled=False))

    assert disabled.bypass is True
    assert disabled.routed is False
    assert disabled.selected_model is None
    assert disabled.reason_codes == ("disabled",)


def test_explicit_task_pin_has_priority_over_auto_routing():
    decision = route_task_model(
        _metadata(
            explicit_pin=True,
            model_override="gpt-5.5",
            cross_file=True,
            high_value=True,
            terra_insufficient=True,
        )
    )

    assert decision.explicit_pin is True
    assert decision.bypass is True
    assert decision.routed is False
    assert decision.selected_model is None
    assert decision.reason_codes == ("explicit_pin",)


def test_non_luna_baseline_is_not_rewritten():
    decision = route_task_model(_metadata(model="gpt-5.6-terra", cross_file=True))

    assert decision.bypass is True
    assert decision.routed is False
    assert decision.selected_provider is None
    assert decision.selected_model is None
    assert decision.reason_codes == ("unsupported_baseline",)


def test_low_risk_terra_criteria_route_to_terra():
    decision = route_task_model(_metadata(cross_file=True, tool_count=3))

    assert decision.routed is True
    assert decision.bypass is False
    assert decision.selected_provider == BASE_PROVIDER
    assert decision.selected_model == TERRA_MODEL
    assert decision.rule == "terra"
    assert decision.reason_codes == ("cross_file", "tool_count_gte_3")


def test_sol_precedence_over_terra():
    decision = route_task_model(
        _metadata(
            cross_file=True,
            high_value=True,
            terra_insufficient=True,
            deep_reasoning_required=True,
        )
    )

    assert decision.routed is True
    assert decision.selected_model == SOL_MODEL
    assert decision.rule == "sol"
    assert "cross_file" not in decision.reason_codes
    assert decision.reason_codes == (
        "high_value",
        "terra_insufficient",
        "deep_reasoning_required",
    )


def test_high_risk_or_external_send_stays_on_luna():
    high_risk = route_task_model(
        _metadata(risk_level="high", cross_file=True, high_value=True, terra_insufficient=True)
    )
    external = route_task_model(
        _metadata(external_send_requested=True, cross_file=True)
    )

    for decision in (high_risk, external):
        assert decision.routed is False
        assert decision.bypass is True
        assert decision.selected_provider == BASE_PROVIDER
        assert decision.selected_model == LUNA_MODEL
    assert high_risk.reason_codes == ("risk_not_low",)
    assert external.reason_codes == ("external_send_requested",)


@pytest.mark.parametrize("risk_level", ["medium", "high"])
def test_non_low_risk_stays_on_luna_even_with_sol_signals(risk_level):
    decision = route_task_model(
        _metadata(
            risk_level=risk_level,
            high_value=True,
            terra_insufficient=True,
            deep_reasoning_required=True,
        )
    )

    assert decision.bypass is True
    assert decision.selected_provider == BASE_PROVIDER
    assert decision.selected_model == LUNA_MODEL
    assert decision.reason_codes == ("risk_not_low",)


def test_external_send_stays_on_luna_even_with_sol_signals():
    decision = route_task_model(
        _metadata(
            external_send_requested=True,
            high_value=True,
            terra_insufficient=True,
            deep_reasoning_required=True,
        )
    )

    assert decision.bypass is True
    assert decision.selected_model == LUNA_MODEL
    assert decision.reason_codes == ("external_send_requested",)


@pytest.mark.parametrize(
    ("trigger", "value"),
    [
        ("cross_file", True),
        ("tool_count", 3),
        ("multi_step_verification", True),
        ("luna_insufficiency", True),
    ],
)
def test_each_terra_trigger_routes_to_terra(trigger, value):
    decision = route_task_model(_metadata(**{trigger: value}))

    assert decision.routed is True
    assert decision.selected_model == TERRA_MODEL
    assert decision.rule == "terra"
    assert decision.reason_codes == (
        "tool_count_gte_3" if trigger == "tool_count" else trigger,
    )


def test_terra_tool_count_boundary_two_does_not_route():
    decision = route_task_model(_metadata(tool_count=2))

    assert decision.bypass is True
    assert decision.selected_model == LUNA_MODEL
    assert decision.reason_codes == ("luna_default",)


@pytest.mark.parametrize(
    ("signal", "expected_reason"),
    [("terra_insufficient", "terra_insufficient"), ("deep_reasoning_required", "deep_reasoning_required")],
)
def test_each_sol_trigger_routes_to_sol(signal, expected_reason):
    decision = route_task_model(_metadata(high_value=True, **{signal: True}))

    assert decision.routed is True
    assert decision.selected_model == SOL_MODEL
    assert decision.rule == "sol"
    assert decision.reason_codes == ("high_value", expected_reason)


def test_event_payload_is_allowlisted_and_prompt_free():
    prompt = "secret task prompt that must not be copied"
    decision = route_task_model(
        _metadata(cross_file=True, title=prompt, body=prompt, prompt=prompt)
    )
    payload = decision.to_event_payload()

    assert payload["selected_model"] == TERRA_MODEL
    assert payload["route_version"] == ROUTE_VERSION
    assert set(payload) == {
        "selected_provider",
        "selected_model",
        "routed",
        "rule",
        "reason_codes",
        "explicit_pin",
        "bypass",
        "route_version",
    }
    assert prompt not in json.dumps(payload, ensure_ascii=False)


def test_router_does_not_mutate_metadata():
    metadata = _metadata(cross_file=True, tool_count=3)
    before = dict(metadata)

    route_task_model(metadata)

    assert metadata == before


def test_routing_metadata_validation_is_strict():
    valid = {
        "enabled": True,
        "risk_level": "low",
        "external_send_requested": False,
        "cross_file": True,
        "tool_count": 0,
        "multi_step_verification": False,
        "luna_insufficiency": False,
        "high_value": False,
        "terra_insufficient": False,
        "deep_reasoning_required": False,
    }
    assert validate_routing_metadata(valid) == valid
    assert validate_routing_metadata(None) is None

    invalid = (
        [],
        "metadata",
        {"unknown": True},
        {"enabled": "true"},
        {"risk_level": "critical"},
        {"tool_count": True},
        {"tool_count": -1},
    )
    for metadata in invalid:
        try:
            validate_routing_metadata(metadata)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid metadata accepted: {metadata!r}")
