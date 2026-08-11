from agent.gemini_native_adapter import translate_gemini_response, translate_stream_event
from agent.turn_finalizer import _turn_completed_successfully


def test_gemini_malformed_function_call_finish_reason_is_preserved():
    response = translate_gemini_response(
        {
            "candidates": [
                {
                    "content": {"parts": []},
                    "finishReason": "MALFORMED_FUNCTION_CALL",
                }
            ]
        },
        model="gemini-2.5-pro",
    )

    assert response.choices[0].finish_reason == "malformed_function_call"


def test_gemini_stream_malformed_function_call_finish_reason_is_preserved():
    chunks = translate_stream_event(
        {
            "candidates": [
                {
                    "content": {"parts": []},
                    "finishReason": "MALFORMED_FUNCTION_CALL",
                }
            ]
        },
        model="gemini-2.5-pro",
        tool_call_indices={},
    )

    assert len(chunks) == 1
    assert chunks[0].choices[0].finish_reason == "malformed_function_call"


def test_gemini_malformed_function_call_overrides_parsed_tool_call():
    response = translate_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "run_shell",
                                    "args": {"command": "echo hi"},
                                }
                            }
                        ]
                    },
                    "finishReason": "MALFORMED_FUNCTION_CALL",
                }
            ]
        },
        model="gemini-2.5-pro",
    )

    assert response.choices[0].finish_reason == "malformed_function_call"


def test_malformed_function_call_failed_turn_is_not_completed():
    assert not _turn_completed_successfully(
        final_response="Gemini returned MALFORMED_FUNCTION_CALL",
        failed=True,
        api_call_count=1,
        max_iterations=5,
        turn_exit_reason="malformed_function_call",
    )


def test_text_response_turn_still_completes_at_iteration_limit():
    assert _turn_completed_successfully(
        final_response="done",
        failed=False,
        api_call_count=5,
        max_iterations=5,
        turn_exit_reason="text_response(finish_reason=stop)",
    )
