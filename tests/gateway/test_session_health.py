from __future__ import annotations

from gateway.session_health import (
    count_session_activity,
    evaluate_session_health,
    plan_session_health_delivery,
    session_health_can_deliver,
    session_health_turn_failed,
)


def _config(**overrides):
    health = {
        "enabled": True,
        "platforms": ["telegram"],
        "min_messages": 80,
        "min_tool_calls": 25,
        "min_prompt_tokens": 72_000,
        "min_context_ratio": 0.45,
        "min_compressions": 2,
        "min_failure_streak": 2,
        "min_signals": 2,
        "strong_signals": 3,
        "cooldown_seconds": 86_400,
        "max_suggestions": 2,
    }
    health.update(overrides)
    return {"gateway": {"session_health": health}}


def _evaluate(*, config=None, state=None, now=1_000, **overrides):
    values = {
        "user_config": config or _config(),
        "platform_key": "telegram",
        "message_count": 0,
        "tool_call_count": 0,
        "session_age_seconds": 0,
        "prompt_tokens": 0,
        "context_length": 0,
        "agent_failed": False,
        "can_deliver": True,
        "compressed": False,
        "state": state or {},
        "now": now,
    }
    values.update(overrides)
    return evaluate_session_health(**values)


def test_long_tool_heavy_telegram_session_gets_one_soft_suggestion():
    decision = _evaluate(message_count=120, tool_call_count=40)

    assert decision.should_suggest is True
    assert decision.level == "soft"
    assert "send /new" in decision.message
    assert "Continue here for follow-ups" in decision.message
    assert "does not delete" in decision.message
    assert decision.next_state["suggestion_count"] == 1
    assert decision.next_state["last_suggested_at"] == 1_000


def test_one_signal_alone_does_not_interrupt_the_client():
    decision = _evaluate(message_count=120, tool_call_count=2)

    assert decision.should_suggest is False
    assert decision.message == ""
    assert decision.next_state["suggestion_count"] == 0


def test_session_age_is_a_signal_but_never_a_sole_trigger():
    aged_only = _evaluate(session_age_seconds=130_000)
    aged_long = _evaluate(message_count=120, session_age_seconds=130_000)

    assert aged_only.should_suggest is False
    assert aged_only.signals == ("old_session",)
    assert aged_long.should_suggest is True
    assert aged_long.signals == ("long_conversation", "old_session")


def test_configuration_cannot_reduce_two_signal_minimum():
    decision = _evaluate(
        config=_config(min_signals=1),
        message_count=120,
        tool_call_count=2,
    )

    assert decision.should_suggest is False
    assert decision.signals == ("long_conversation",)


def test_disabled_and_non_telegram_paths_never_suggest():
    disabled = _evaluate(
        config=_config(enabled=False), message_count=200, tool_call_count=100
    )
    discord = _evaluate(platform_key="discord", message_count=200, tool_call_count=100)
    configured_discord = _evaluate(
        config=_config(platforms=["discord"]),
        platform_key="discord",
        message_count=200,
        tool_call_count=100,
    )

    assert disabled.should_suggest is False
    assert discord.should_suggest is False
    assert configured_discord.should_suggest is False


def test_failed_turn_updates_failure_streak_but_never_adds_advice():
    first = _evaluate(
        message_count=120,
        tool_call_count=40,
        agent_failed=True,
    )
    second = _evaluate(
        message_count=120,
        tool_call_count=40,
        agent_failed=True,
        state=first.next_state,
        now=1_100,
    )

    assert first.should_suggest is False
    assert second.should_suggest is False
    assert second.next_state["failure_streak"] == 2


def test_interrupted_partial_incomplete_error_or_empty_turns_are_failed():
    unsuccessful = (
        ({"failed": True}, "response", False),
        ({"interrupted": True}, "response", False),
        ({"partial": True}, "response", False),
        ({"completed": False}, "response", False),
        ({"error": "provider timeout"}, "response", False),
        ({"completed": True, "api_calls": 1}, "", False),
        ({}, "", False),
        ({"final_response": "(empty)"}, "(empty)", False),
        ({"completed": True}, "internal retry sentinel", True),
    )

    assert all(
        session_health_turn_failed(result, final_response, gateway_error=gateway_error)
        for result, final_response, gateway_error in unsuccessful
    )
    assert (
        session_health_turn_failed({"completed": True, "already_sent": True}, "")
        is False
    )
    assert session_health_turn_failed({"completed": True}, "Done.") is False


def test_successful_turn_can_use_persisted_failure_and_compression_signals():
    state = {
        "suggestion_count": 0,
        "last_suggested_at": 0,
        "failure_streak": 2,
        "compression_count": 1,
    }

    decision = _evaluate(
        message_count=10,
        tool_call_count=1,
        compressed=True,
        state=state,
    )

    assert decision.should_suggest is True
    assert decision.level == "soft"
    assert decision.signals == ("compression", "recent_failures")
    assert decision.next_state["failure_streak"] == 0
    assert decision.next_state["compression_count"] == 2


def test_cooldown_preserves_fractional_timestamp_precision():
    state = {
        "suggestion_count": 1,
        "last_suggested_at": 1_000.9,
        "failure_streak": 0,
        "compression_count": 0,
    }

    just_before_full_cooldown = _evaluate(
        message_count=200,
        tool_call_count=100,
        prompt_tokens=90_000,
        state=state,
        now=87_400.5,
    )
    at_full_cooldown = _evaluate(
        message_count=200,
        tool_call_count=100,
        prompt_tokens=90_000,
        state=state,
        now=87_400.9,
    )

    assert just_before_full_cooldown.should_suggest is False
    assert just_before_full_cooldown.next_state["last_suggested_at"] == 1_000.9
    assert at_full_cooldown.should_suggest is True


def test_cooldown_and_maximum_prevent_repeated_nagging():
    initial = _evaluate(message_count=120, tool_call_count=40)
    during_cooldown = _evaluate(
        message_count=200,
        tool_call_count=100,
        prompt_tokens=90_000,
        state=initial.next_state,
        now=1_100,
    )
    same_health_after_cooldown = _evaluate(
        message_count=120,
        tool_call_count=40,
        state=initial.next_state,
        now=100_000,
    )
    after_cooldown = _evaluate(
        message_count=200,
        tool_call_count=100,
        prompt_tokens=90_000,
        state=initial.next_state,
        now=100_000,
    )
    after_maximum = _evaluate(
        message_count=300,
        tool_call_count=150,
        prompt_tokens=100_000,
        state=after_cooldown.next_state,
        now=200_000,
    )

    assert during_cooldown.should_suggest is False
    assert same_health_after_cooldown.should_suggest is False
    assert after_cooldown.should_suggest is True
    assert after_cooldown.level == "strong"
    assert after_maximum.should_suggest is False
    assert after_maximum.next_state["suggestion_count"] == 2


def test_configuration_cannot_remove_cooldown_or_exceed_two_suggestions():
    unsafe_config = _config(
        cooldown_seconds=0,
        max_suggestions=5,
        strong_signals=1,
    )
    health = {
        "config": unsafe_config,
        "message_count": 200,
        "tool_call_count": 100,
        "prompt_tokens": 90_000,
    }

    first = _evaluate(**health)
    immediate_second = _evaluate(**health, state=first.next_state, now=1_001)
    second = _evaluate(**health, state=first.next_state, now=100_000)
    third = _evaluate(**health, state=second.next_state, now=200_000)

    assert first.should_suggest is True
    assert immediate_second.should_suggest is False
    assert second.should_suggest is True
    assert second.level == "strong"
    assert third.should_suggest is False
    assert third.next_state["suggestion_count"] == 2


def test_prompt_ratio_is_bounded_and_counts_as_one_signal():
    below = _evaluate(
        message_count=120,
        prompt_tokens=40_000,
        context_length=100_000,
    )
    above = _evaluate(
        message_count=120,
        prompt_tokens=50_000,
        context_length=100_000,
    )

    assert below.should_suggest is False
    assert above.should_suggest is True
    assert above.signals == ("long_conversation", "context_pressure")


def test_malformed_config_fails_closed_without_raising():
    decision = _evaluate(
        config={"gateway": {"session_health": "yes please"}},
        message_count=200,
        tool_call_count=100,
    )

    assert decision.should_suggest is False
    assert decision.message == ""


def test_gateway_session_health_is_a_known_config_section():
    from hermes_cli.config import _validate_config_key

    for key in (
        "gateway.session_health.enabled",
        "gateway.session_health.min_age_seconds",
        "gateway.session_health.min_signals",
        "gateway.session_health.cooldown_seconds",
        "gateway.session_health.max_suggestions",
    ):
        is_known, suggestion = _validate_config_key(key)
        assert is_known, f"{key} should be canonical, got suggestion={suggestion!r}"


def test_zero_thresholds_disable_signals_instead_of_matching_every_turn():
    decision = _evaluate(
        config=_config(
            min_messages=0,
            min_tool_calls=0,
            min_prompt_tokens=0,
            min_context_ratio=0,
            min_compressions=0,
            min_failure_streak=0,
        )
    )

    assert decision.should_suggest is False
    assert decision.signals == ()


def test_activity_counter_ignores_system_metadata_and_counts_completed_tools():
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "session_meta", "tools": [{"name": "terminal"}]},
        {"role": "user", "content": "check this"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal"}},
                {"id": "call-2", "function": {"name": "read_file"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "tool", "tool_call_id": "call-2", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]

    message_count, tool_call_count = count_session_activity(messages)

    assert message_count == 3
    assert tool_call_count == 2


def test_activity_counter_fails_closed_for_malformed_entries():
    message_count, tool_call_count = count_session_activity([
        None,
        "bad",
        {},
        {"role": "tool"},
        {"role": "user"},
    ])

    assert message_count == 1
    assert tool_call_count == 1


def test_delivery_plan_appends_only_for_non_streamed_reply():
    normal = plan_session_health_delivery(
        response="Finished.", advice="Use /new.", already_sent=False
    )
    streamed = plan_session_health_delivery(
        response="Finished.", advice="Use /new.", already_sent=True
    )

    assert normal.response == "Finished.\n\nUse /new."
    assert normal.trailing_message == ""
    assert streamed.response == "Finished."
    assert streamed.trailing_message == "Use /new."


def test_delivery_plan_suppresses_advice_without_visible_response():
    delivery = plan_session_health_delivery(
        response="", advice="Use /new.", already_sent=False
    )

    assert delivery.response == ""
    assert delivery.trailing_message == ""


def test_streamed_empty_final_response_can_deliver_but_silence_cannot():
    assert session_health_can_deliver(
        response="", already_sent=True, intentional_silence=False
    )
    streamed = plan_session_health_delivery(
        response="", advice="Use /new.", already_sent=True
    )
    assert streamed.response == ""
    assert streamed.trailing_message == "Use /new."
    assert not session_health_can_deliver(
        response="", already_sent=False, intentional_silence=False
    )
    assert not session_health_can_deliver(
        response="", already_sent=True, intentional_silence=True
    )


def test_unsurfaceable_turn_does_not_consume_notice_quota_or_cooldown():
    decision = _evaluate(
        message_count=120,
        tool_call_count=40,
        can_deliver=False,
    )

    assert decision.should_suggest is False
    assert decision.next_state["suggestion_count"] == 0
    assert decision.next_state["last_suggested_at"] == 0
