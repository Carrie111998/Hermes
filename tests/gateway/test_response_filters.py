from gateway.response_filters import (
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_structured_external_delivery_suppresses_even_if_marker_is_transformed():
    result = {
        "failed": False,
        "completed": True,
        "turn_exit_reason": "external_delivery_complete",
        "delivery_already_sent": True,
        "external_deliveries": [{"protocol": "hermes.external_delivery"}],
    }
    assert is_intentional_silence_agent_result(result, "unexpected transformed text")

    for invalid in (
        {**result, "completed": False},
        {**result, "turn_exit_reason": "text_response(finish_reason=stop)"},
        {**result, "external_deliveries": []},
        {**result, "failed": True},
    ):
        assert not is_intentional_silence_agent_result(
            invalid,
            "unexpected transformed text",
        )


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


