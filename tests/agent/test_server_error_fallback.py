"""Regression tests for eager fallback on plain server_error (500/502).

_is_eager_fallback_transport_reason() decides whether an error reason
qualifies for the main retry loop's eager-fallback gate (one retry, then
fail over to fallback_providers) rather than only reaching a configured
fallback chain via the separate max-retry-exhaustion attempt later in the
same loop. server_error (500/502) joining timeout/overloaded here means
that failover happens sooner — after 2 retries — instead of waiting out
the full retry budget on a primary that's already failing.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import conversation_loop
from agent.conversation_loop import _is_eager_fallback_transport_reason
from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def test_server_error_is_eager_fallback_eligible():
    assert _is_eager_fallback_transport_reason(FailoverReason.server_error) is True


def test_overloaded_is_eager_fallback_eligible():
    assert _is_eager_fallback_transport_reason(FailoverReason.overloaded) is True


def test_timeout_is_eager_fallback_eligible():
    assert _is_eager_fallback_transport_reason(FailoverReason.timeout) is True


def test_rate_limit_is_not_transport_eligible():
    # rate_limit takes the separate, immediate is_rate_limited path in the
    # loop's _should_fallback gate - it must not double-count here.
    assert _is_eager_fallback_transport_reason(FailoverReason.rate_limit) is False


def test_billing_is_not_transport_eligible():
    assert _is_eager_fallback_transport_reason(FailoverReason.billing) is False


def test_unknown_is_not_transport_eligible():
    assert _is_eager_fallback_transport_reason(FailoverReason.unknown) is False


def test_should_fallback_activates_at_retry_count_two():
    """Reproduces the loop's _should_fallback expression directly: eager
    fallback for a transport/server-error reason requires retry_count >= 2,
    matching the existing overloaded/timeout threshold - not retry 0 or 1."""
    is_rate_limited = False  # server_error never sets this
    for retry_count in (0, 1):
        _is_transport_failure = _is_eager_fallback_transport_reason(FailoverReason.server_error)
        should_fallback = is_rate_limited or (_is_transport_failure and retry_count >= 2)
        assert should_fallback is False, f"retry_count={retry_count} must not eager-fallback yet"

    _is_transport_failure = _is_eager_fallback_transport_reason(FailoverReason.server_error)
    should_fallback = is_rate_limited or (_is_transport_failure and 2 >= 2)
    assert should_fallback is True


def test_run_conversation_uses_extracted_reason_helper():
    """The loop's eager-fallback gate must actually call the extracted,
    unit-tested helper rather than an inline literal set that could drift
    out of sync with it (e.g. a future edit adding a reason to one but not
    the other)."""
    source = inspect.getsource(conversation_loop.run_conversation)

    assert "_is_transport_failure = _is_eager_fallback_transport_reason(classified.reason)" in source
    assert "retry_count >= 2" in source


class _MockServerError(Exception):
    """Simulates an OpenAI SDK APIStatusError for a plain 500/502."""

    def __init__(self, status_code):
        super().__init__(f"Error code: {status_code} - internal server error")
        self.status_code = status_code
        self.body = {"error": {"message": "internal server error"}}


def _mock_response(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="glm-4.7", usage=None)


def _make_agent_with_fallback(fb_chain):
    """Build a minimal AIAgent with the given fallback chain configured.

    Mirrors ``tests/run_agent/test_32646_fallback_429_after_timeout.py``'s
    ``_make_agent_with_fallback`` helper.
    """
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider="zai",
            model="glm-5.1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
        )
        agent.client = MagicMock()
        return agent


def test_run_conversation_activates_fallback_after_two_server_errors():
    """Full loop regression for PR #58355's requested coverage.

    Both teknium1's original review and the 2026-07-29 automated cross-PR
    triage flagged that only the extracted helper and an
    ``inspect.getsource`` wiring check existed — no test drove the actual
    ``run_conversation`` loop. This reproduces the real scenario: two
    plain HTTP 500s on the primary provider, then a successful response
    from the configured ``fallback_providers`` entry, asserting fallback
    activates after the second failure (``retry_count >= 2``) rather than
    waiting for the max-retry-exhaustion path.
    """
    fb_chain = [
        {
            "provider": "zai",
            "model": "glm-4.7",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        }
    ]
    agent = _make_agent_with_fallback(fb_chain)
    agent._api_max_retries = 2

    calls = []

    def fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        attempt = len(calls)
        if attempt <= 2:
            raise _MockServerError(500)
        return _mock_response("Recovered via fallback")

    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "primary-key-abcdef12"
    mock_fb_client.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch.object(conversation_loop, "jittered_backoff", return_value=0.0),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb_client, "glm-4.7"),
        ) as mock_resolve,
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered via fallback"
    assert calls == [
        ("zai", "glm-5.1"),
        ("zai", "glm-5.1"),
        ("zai", "glm-4.7"),
    ]
    mock_resolve.assert_called_once()
    assert agent._fallback_activated is True
    assert agent.model == "glm-4.7"
