from gateway.response_filters import (
    is_internal_gateway_error_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_edge_punctuation_silence_tokens_are_intentional_silence():
    for token in (".NO_REPLY", "*NO_REPLY*", " .NO_REPLY ", "*[SILENT]*", "NO_REPLY."):
        assert is_intentional_silence_response(token)


def test_blank_and_prose_mentions_are_not_silence():
    assert not is_intentional_silence_response("")
    assert not is_intentional_silence_response("Use NO_REPLY when no answer is needed.")
    assert not is_intentional_silence_response("The reply was [SILENT], intentionally.")
    assert not is_intentional_silence_response("😄 NO_REPLY")
    assert not is_intentional_silence_response("[SILENT")


def test_failed_agent_result_never_counts_as_intentional_silence():
    assert is_intentional_silence_agent_result({"failed": False}, "NO_REPLY")
    assert not is_intentional_silence_agent_result({"failed": True}, "NO_REPLY")


def test_internal_gateway_errors_are_detected_without_suppressing_business_warnings():
    assert is_internal_gateway_error_response(
        "⚠️ The model provider failed after retries. Check gateway logs."
    )
    assert is_internal_gateway_error_response(
        "Sorry, I encountered an unexpected error. Try again."
    )
    assert is_internal_gateway_error_response(
        "ℹ Codex gpt-5.6-luna caps context at 272K, so auto-compaction was raised."
    )
    assert not is_internal_gateway_error_response(
        "⚠️ Heavy rain is forecast. We can reschedule your pest treatment."
    )
