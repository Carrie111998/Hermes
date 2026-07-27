import pytest

from agent.model_routing import resolve_routing_decision


def _routing_cfg(**overrides):
    cfg = {
        "routing": {
            "enabled": True,
            "default_profile": "balanced",
            "allow_escalation": True,
        }
    }
    cfg["routing"].update(overrides)
    return cfg


def _decide(message: str, *, cfg=None, base_model="gpt-5.6-sol", fallback_reasoning=None, **kwargs):
    if fallback_reasoning is None:
        fallback_reasoning = {"enabled": True, "effort": "medium"}
    return resolve_routing_decision(
        message=message,
        base_model=base_model,
        fallback_reasoning_config=fallback_reasoning,
        config=cfg if cfg is not None else _routing_cfg(),
        surface="test",
        **kwargs,
    )


def test_trivial_fact_routes_to_luna_low():
    decision = _decide("what time is it in tokyo")
    assert decision.profile == "fast"
    assert decision.model == "gpt-5.6-luna"
    assert decision.reasoning_config == {"enabled": True, "effort": "low"}


def test_saved_workflow_execution_routes_to_luna_low():
    decision = _decide("run the youtube editor handoff workflow")
    assert decision.profile == "fast"
    assert decision.category == "workflow.known"
    assert decision.model == "gpt-5.6-luna"
    assert decision.reasoning_config == {"enabled": True, "effort": "low"}


def test_ambiguous_trivial_can_route_to_luna_medium():
    decision = _decide("find email from John where multiple matches might exist")
    assert decision.profile == "fast_plus"
    assert decision.model == "gpt-5.6-luna"
    assert decision.reasoning_config == {"enabled": True, "effort": "medium"}


def test_new_automation_work_routes_to_terra_medium():
    decision = _decide("build a new automation for lead tagging")
    assert decision.profile == "balanced"
    assert decision.model == "gpt-5.6-terra"
    assert decision.reasoning_config == {"enabled": True, "effort": "medium"}


def test_github_work_routes_to_terra_medium():
    decision = _decide("make the requested GitHub changes")
    assert decision.profile == "balanced"
    assert decision.model == "gpt-5.6-terra"
    assert decision.reasoning_config == {"enabled": True, "effort": "medium"}


def test_youtube_packet_routes_to_terra_high():
    decision = _decide("build a YouTube video packet with title and thumbnail options")
    assert decision.profile == "creative"
    assert decision.model == "gpt-5.6-terra"
    assert decision.reasoning_config == {"enabled": True, "effort": "high"}


def test_launch_strategy_routes_to_sol_high():
    decision = _decide("build our launch strategy for next quarter")
    assert decision.profile == "strong"
    assert decision.model == "gpt-5.6-sol"
    assert decision.reasoning_config == {"enabled": True, "effort": "high"}


def test_failed_terra_work_escalates_to_sol():
    decision = _decide("terra failed twice, solve this")
    assert decision.profile == "strong"
    assert decision.model == "gpt-5.6-sol"
    assert decision.reasoning_config == {"enabled": True, "effort": "high"}


def test_explicit_override_wins_over_classification():
    decision = _decide(
        "[[route: luna xhigh no-escalation]] build our launch strategy",
    )
    assert decision.override_used is True
    assert decision.no_escalation is True
    assert decision.profile == "fast"
    assert decision.model == "gpt-5.6-luna"
    assert decision.reasoning_config == {"enabled": True, "effort": "xhigh"}


def test_workflow_model_pin_wins_over_auto_classification():
    decision = _decide(
        "build a launch strategy",
        workflow_model_override="gpt-5.6-luna",
    )
    assert decision.override_used is False
    assert decision.category == "workflow.model_pin"
    assert decision.model == "gpt-5.6-luna"


def test_explicit_override_beats_workflow_pin():
    decision = _decide(
        "[[route: sol high]] simple follow-up",
        workflow_model_override="gpt-5.6-luna",
    )
    assert decision.override_used is True
    assert decision.model == "gpt-5.6-sol"
    assert decision.reasoning_config == {"enabled": True, "effort": "high"}


def test_no_escalation_override_blocks_risk_escalation():
    decision = _decide("[[route: no-escalation]] publish publicly and deploy production")
    assert decision.profile != "strong"
    assert decision.escalation_reason is None


def test_risk_escalation_upgrades_to_strong():
    decision = _decide("publish publicly and deploy production")
    assert decision.profile == "strong"
    assert decision.category == "escalation.risk"


def test_routing_disabled_keeps_base_model_and_reasoning():
    decision = _decide(
        "build a launch strategy",
        cfg={"routing": {"enabled": False}},
        base_model="gpt-5.6-terra",
        fallback_reasoning={"enabled": True, "effort": "low"},
    )
    assert decision.profile == "session"
    assert decision.model == "gpt-5.6-terra"
    assert decision.reasoning_config == {"enabled": True, "effort": "low"}


def test_inline_override_is_stripped_from_clean_message():
    decision = _decide("[[route: luna medium]] summarize this")
    assert "[[route:" not in decision.clean_message.lower()
    assert decision.clean_message == "summarize this"


def test_legacy_turn_routing_bridge_when_new_profiles_missing():
    cfg = {
        "routing": {"enabled": True},
        "agent": {
            "turn_routing": {
                "enabled": True,
                "trivial_model": "gpt-5.5",
                "trivial_reasoning": "none",
            }
        },
    }
    decision = _decide("what time is it", cfg=cfg)
    assert decision.model == "gpt-5.5"
    assert decision.reasoning_config == {"enabled": False}


def test_legacy_turn_routing_bridge_not_applied_when_profiles_present():
    cfg = {
        "routing": {
            "enabled": True,
            "profiles": {
                "fast": {"model": "luna", "reasoning": "low"},
            },
        },
        "agent": {
            "turn_routing": {
                "enabled": True,
                "trivial_model": "gpt-5.5",
                "trivial_reasoning": "none",
            }
        },
    }
    decision = _decide("what time is it", cfg=cfg, base_model="gpt-5.6-sol")
    assert decision.model == "gpt-5.6-luna"
    assert decision.reasoning_config == {"enabled": True, "effort": "low"}
