from gateway.response_filters import (
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    should_suppress_delivery,
    validated_delivery_outcome,
)


def _result(*, action="suppress", reason="nothing new", failed=False):
    return {
        "failed": failed,
        "turn_id": "turn-1",
        "delivery_outcome": {
            "action": action,
            "reason": reason,
            "turn_id": "turn-1",
        },
    }


def test_exact_same_turn_structured_suppress_is_executed():
    assert validated_delivery_outcome(_result()) == {
        "action": "suppress",
        "reason": "nothing new",
        "turn_id": "turn-1",
    }
    assert should_suppress_delivery(_result()) is True
    assert should_suppress_delivery(_result(action="deliver")) is False


def test_failed_or_unknown_turn_status_always_delivers():
    assert should_suppress_delivery(_result(failed=True)) is False
    unknown = _result()
    unknown.pop("failed")
    assert should_suppress_delivery(unknown) is False


def test_stale_or_malformed_outcome_always_delivers():
    stale = _result()
    stale["delivery_outcome"]["turn_id"] = "turn-0"
    assert should_suppress_delivery(stale) is False

    extra = _result()
    extra["delivery_outcome"]["extra"] = True
    assert should_suppress_delivery(extra) is False

    empty_reason = _result(reason="   ")
    assert should_suppress_delivery(empty_reason) is False


def test_response_text_has_no_delivery_authority():
    for text in ("[SILENT]", "NO_REPLY", ".NO_REPLY", "ordinary report"):
        result = {
            "failed": False,
            "turn_id": "turn-1",
            "delivery_outcome": None,
            "final_response": text,
        }
        assert should_suppress_delivery(result) is False


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


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_autonomous_silence_still_delivers_mid_sentence_mentions():
    assert not is_autonomous_silence_response(
        "I considered staying [SILENT] but this one moved money, so: refunded $240."
    )
    assert not is_autonomous_silence_response("Silent retry succeeded; all good.")
    assert not is_autonomous_silence_response("")
    assert not is_autonomous_silence_response(None)
