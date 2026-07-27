from agent.response_hooks import (
    DEFAULT_MAX_RESPONSE_CONTINUATIONS,
    max_response_continuations,
)


def test_default_max_response_continuations_is_one():
    assert DEFAULT_MAX_RESPONSE_CONTINUATIONS == 1
    assert max_response_continuations({}) == 1


def test_configured_max_response_continuations_is_non_negative():
    assert max_response_continuations(
        {"agent": {"max_response_continuations": 2}}
    ) == 2
    assert max_response_continuations(
        {"agent": {"max_response_continuations": -1}}
    ) == 0


def test_invalid_max_response_continuations_uses_default():
    assert max_response_continuations(
        {"agent": {"max_response_continuations": "invalid"}}
    ) == DEFAULT_MAX_RESPONSE_CONTINUATIONS
