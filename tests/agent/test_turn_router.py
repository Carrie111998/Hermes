import pytest

from agent.turn_router import (
    RouteAuthorization,
    RouteDecision,
    authorize_route,
    decide_turn_route,
    enforce_hard_budget_target,
    hard_budget_slot_count,
    normalize_turn_routing_config,
)


def test_normalize_turn_routing_config_defaults_to_off_and_current_route():
    config = normalize_turn_routing_config(None)

    assert config["mode"] == "off"
    assert config["default_route"] == "current"
    assert config["routes"] == {
        "current": {"kind": "current", "enabled": True},
    }
    assert config["classifier"]["enabled"] is False


def test_normalize_turn_routing_config_preserves_valid_model_and_moa_routes():
    config = normalize_turn_routing_config(
        {
            "mode": "AUTO",
            "default_route": "k3",
            "routes": {
                "k3": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                },
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "classifier": {
                "enabled": "true",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "timeout_seconds": "4.5",
                "min_confidence": "0.8",
            },
        }
    )

    assert config["mode"] == "auto"
    assert config["default_route"] == "k3"
    assert config["routes"]["k3"] == {
        "kind": "model",
        "provider": "kimi-coding",
        "model": "k3",
        "enabled": True,
    }
    assert config["routes"]["deep"] == {
        "kind": "moa",
        "preset": "deep",
        "enabled": True,
    }
    assert config["classifier"] == {
        "enabled": True,
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "timeout_seconds": 4.5,
        "min_confidence": 0.8,
    }


def test_decide_turn_route_is_strictly_inert_when_mode_is_off():
    decision = decide_turn_route(
        "设计一个跨 Desktop、CLI 和 Gateway 的高风险核心架构迁移",
        {
            "mode": "off",
            "default_route": "deep",
            "routes": {
                "deep": {"kind": "moa", "preset": "deep"},
            },
        },
    )

    assert decision == RouteDecision(
        route="current",
        target={"kind": "current", "enabled": True},
        mode="off",
        source="configured",
        reason_code="routing_off",
        confidence=1.0,
        should_apply=False,
        requires_confirmation=False,
    )


def test_observe_mode_recommends_deep_lane_without_applying_it():
    decision = decide_turn_route(
        "设计一个跨 Desktop、CLI 和 Gateway 的高风险核心架构迁移",
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep-moa": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep-moa"},
        },
    )

    assert decision.route == "deep-moa"
    assert decision.target == {"kind": "moa", "preset": "deep", "enabled": True}
    assert decision.mode == "observe"
    assert decision.source == "rule"
    assert decision.reason_code == "architecture_complexity"
    assert decision.confidence == 0.9
    assert decision.should_apply is False


def test_auto_mode_marks_configured_deep_route_for_application():
    decision = decide_turn_route(
        "Architect a high-risk cross-system migration",
        {
            "mode": "auto",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep-moa": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep-moa"},
        },
    )

    assert decision.route == "deep-moa"
    assert decision.mode == "auto"
    assert decision.should_apply is True


def test_route_decision_target_is_deeply_immutable_and_configured_source_is_normalized():
    decision = decide_turn_route(
        "Summarize this note",
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                }
            },
        },
    )

    assert decision.source == "configured"
    with pytest.raises(TypeError):
        decision.target["model"] = "mutated"


def test_illegal_or_disabled_configured_routes_fall_back_to_current_without_apply():
    config = normalize_turn_routing_config(
        {
            "mode": "auto",
            "default_route": "illegal",
            "routes": {
                "illegal": {"kind": "shell", "command": "never"},
                "disabled": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                    "enabled": False,
                },
            },
            "lanes": {"plain": "disabled", "deep": "illegal"},
        }
    )

    assert config["default_route"] == "current"
    assert config["lanes"] == {"plain": "current"}
    decision = decide_turn_route("Summarize this note", config)
    assert decision.route == "current"
    assert decision.source == "configured"
    assert decision.should_apply is False


def test_budgeted_route_metadata_survives_normalization_for_independent_authorization():
    config = normalize_turn_routing_config(
        {
            "mode": "observe",
            "default_route": "grok-review",
            "routes": {
                "grok-review": {
                    "kind": "model",
                    "provider": "xai",
                    "model": "grok-4.5",
                    "budgeted": True,
                }
            },
        }
    )

    assert config["routes"]["grok-review"]["budgeted"] is True


@pytest.mark.parametrize("source", ["rule", "classifier", "explicit"])
def test_budgeted_target_cannot_apply_without_independent_reservation(source):
    selected = RouteDecision(
        route="grok-review",
        target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
            "budgeted": True,
        },
        mode="auto",
        source=source,
        reason_code="selected",
        confidence=1.0,
        should_apply=True,
    )

    denied = authorize_route(selected, None)

    assert denied.should_apply is False
    assert denied.requires_confirmation is True
    assert denied.reason_code == "budget_authorization_required"


@pytest.mark.parametrize(
    "provider,model",
    [
        ("xai", "grok-4.5"),
        ("xai-oauth", "opaque-review-model"),
        ("x-ai-oauth", "opaque-review-model"),
        ("grok-oauth", "opaque-review-model"),
        ("x-ai", "opaque-review-model"),
        ("x.ai", "opaque-review-model"),
        ("openrouter", "x-ai/grok-4.5"),
        ("nous", "grok-review"),
    ],
)
def test_grok_identity_cannot_bypass_budget_gate_when_metadata_omits_budgeted_flag(
    provider,
    model,
):
    selected = RouteDecision(
        route="grok-review",
        target={
            "kind": "model",
            "provider": provider,
            "model": model,
            "budgeted": False,
        },
        mode="auto",
        source="configured",
        reason_code="default_route",
        confidence=1.0,
        should_apply=True,
    )

    denied = authorize_route(
        selected,
        RouteAuthorization(allowed=True, reason_code="entitled"),
    )

    assert denied.target["budgeted"] is True
    assert denied.should_apply is False
    assert denied.requires_confirmation is True
    assert denied.reason_code == "budget_authorization_required"


def test_moa_members_cannot_hide_grok_behind_opaque_preset_name():
    target = enforce_hard_budget_target(
        {"kind": "moa", "preset": "frontier", "budgeted": False},
        moa_config={
            "reference_models": [
                {"provider": "kimi-coding", "model": "k3-256k"},
                {"provider": "xai-oauth", "model": "opaque-review-model"},
            ],
            "aggregator": {"provider": "kimi-coding", "model": "k3"},
        },
    )

    assert target.budgeted is True


def test_hard_budget_slot_count_counts_resolved_grok_references_and_aggregator():
    count = hard_budget_slot_count(
        {"kind": "moa", "preset": "frontier"},
        moa_config={
            "presets": {
                "frontier": {
                    "reference_models": [
                        {"provider": "xai-oauth", "model": "opaque-review"},
                        {"provider": "kimi-coding", "model": "k3-256k"},
                    ],
                    "aggregator": {"provider": "xai", "model": "grok-4.5"},
                }
            }
        },
    )

    assert count == 2


def test_hard_budget_slot_count_fails_closed_when_moa_identity_is_unavailable():
    assert hard_budget_slot_count(
        {"kind": "moa", "preset": "frontier", "budgeted": True},
        moa_config={"_identity_unavailable": True},
    ) is None


def test_hard_gate_denial_overrides_safe_router_recommendation():
    selected = RouteDecision(
        route="deep",
        target={"kind": "moa", "preset": "deep"},
        mode="auto",
        source="rule",
        reason_code="architecture_complexity",
        confidence=0.9,
        should_apply=True,
    )

    denied = authorize_route(
        selected,
        RouteAuthorization(allowed=False, reason_code="target_not_entitled"),
    )

    assert denied.should_apply is False
    assert denied.reason_code == "target_not_entitled"


def test_budgeted_target_applies_only_with_allowed_reservation():
    selected = RouteDecision(
        route="grok-review",
        target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
            "budgeted": True,
        },
        mode="auto",
        source="rule",
        reason_code="review_lane",
        confidence=0.9,
        should_apply=True,
    )

    authorized = authorize_route(
        selected,
        RouteAuthorization(
            allowed=True,
            reason_code="budget_reserved",
            reservation_id="reservation-1",
        ),
    )

    assert authorized.should_apply is True
    assert authorized.reason_code == "review_lane"


@pytest.mark.parametrize(
    "content",
    [
        [
            {"type": "image_url", "image_url": {"url": "data:architecture-high-risk"}},
            {"type": "text", "text": "Architect a high-risk cross-system migration"},
        ],
        [
            {"type": "input_image", "image_url": "data:architecture-high-risk"},
            {"type": "input_text", "text": "设计一个跨系统的高风险架构迁移"},
        ],
    ],
)
def test_multimodal_routing_extracts_only_visible_text_with_language_parity(content):
    decision = decide_turn_route(
        content,
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
    )

    assert decision.route == "deep"
    assert decision.reason_code == "architecture_complexity"


def test_multimodal_image_metadata_cannot_trigger_routing_rules():
    decision = decide_turn_route(
        [
            {
                "type": "image_url",
                "image_url": {"url": "data:architecture-high-risk-cross-system-migration"},
            }
        ],
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
    )

    assert decision.route == "k3"
    assert decision.reason_code == "default_route"


def test_nested_message_content_does_not_route_on_image_or_audio_metadata():
    decision = decide_turn_route(
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:architecture-high-risk-cross-system-migration"
                    },
                },
                {
                    "type": "input_audio",
                    "input_audio": {"id": "architecture-high-risk-migration"},
                },
            ],
        },
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
    )

    assert decision.route == "k3"
    assert decision.reason_code == "default_route"


@pytest.mark.parametrize(
    "text",
    [
        "microarchitecture migration notes",
        "architectureDecision migrationCount highRisk",
        "微架构生产力迁移",
        "Inspect this metadata only:\n```json\n{\"architecture\": \"high-risk migration\"}\n```",
    ],
)
def test_semantic_rule_matcher_rejects_substrings_and_code_metadata(text):
    decision = decide_turn_route(
        text,
        {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
    )

    assert decision.route == "k3"
    assert decision.reason_code == "default_route"
