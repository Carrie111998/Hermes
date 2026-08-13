"""Provider-signalled rate limits wrapped in HTTP 5xx responses."""

from unittest.mock import MagicMock, patch

from agent.auxiliary_client import (
    _is_payment_error,
    _is_rate_limit_error,
    _is_transient_transport_error,
    call_llm,
)


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _call_patches(client):
    return (
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("openrouter", "some-model", None, None, None),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "some-model"),
        ),
        patch(
            "agent.auxiliary_client._validate_llm_response",
            side_effect=lambda response, _task, **_kwargs: response,
        ),
    )


def test_rate_limit_503_is_not_transient_transport_error():
    error = ProviderError(
        "Error code: 503 - Rate limit exceeded. Try again in 4h 23m "
        "(2 rate-limited, 3 model-quota-exhausted)",
        503,
    )

    assert _is_rate_limit_error(error) is True
    assert _is_transient_transport_error(error) is False


def test_generic_503_remains_transient_and_retries_same_provider():
    error = ProviderError("Error code: 503 - upstream server error", 503)
    response = {"ok": True}
    client = MagicMock()
    client.base_url = "https://openrouter.ai/api/v1"
    client.chat.completions.create.side_effect = [error, response]
    p1, p2, p3 = _call_patches(client)

    assert _is_rate_limit_error(error) is False
    assert _is_transient_transport_error(error) is True
    with (
        p1,
        p2,
        p3,
        patch("agent.auxiliary_client._transient_retry_count", return_value=1),
        patch("agent.auxiliary_client.time.sleep"),
    ):
        result = call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )

    assert result is response
    assert client.chat.completions.create.call_count == 2


def test_billing_503_remains_payment_not_rate_limit():
    error = ProviderError(
        "Error code: 503 - Billing account has insufficient credits; "
        "try again in 1h",
        503,
    )

    assert _is_payment_error(error) is True
    assert _is_rate_limit_error(error) is False
    assert _is_transient_transport_error(error) is False


def test_compression_rate_limit_skips_same_provider_retry_before_fallback():
    error = ProviderError(
        "Error code: 503 - Rate limit exceeded. Try again in 4h",
        503,
    )
    primary = MagicMock()
    primary.base_url = "https://openrouter.ai/api/v1"
    primary.chat.completions.create.side_effect = error
    fallback = MagicMock()
    fallback.base_url = "https://api.openai.com/v1"
    fallback.chat.completions.create.return_value = {"fallback": True}
    p1, p2, p3 = _call_patches(primary)

    with (
        p1,
        p2,
        p3,
        patch(
            "agent.auxiliary_client._recoverable_pool_provider",
            return_value=None,
        ),
        patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(None, None, ""),
        ),
        patch(
            "agent.auxiliary_client._try_main_agent_model_fallback",
            return_value=(fallback, "fallback-model", "openai"),
        ),
        patch("agent.auxiliary_client.time.sleep") as sleep,
    ):
        result = call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )

    assert result == {"fallback": True}
    assert primary.chat.completions.create.call_count == 1
    assert fallback.chat.completions.create.call_count == 1
    sleep.assert_not_called()


def test_non_compression_timeout_still_retries_same_provider():
    class RequestTimeout(Exception):
        pass

    error = RequestTimeout("Request timed out")
    response = {"ok": True}
    client = MagicMock()
    client.base_url = "https://openrouter.ai/api/v1"
    client.chat.completions.create.side_effect = [error, response]
    p1, p2, p3 = _call_patches(client)

    with (
        p1,
        p2,
        p3,
        patch("agent.auxiliary_client._transient_retry_count", return_value=1),
        patch("agent.auxiliary_client.time.sleep"),
    ):
        result = call_llm(
            task="title_generation",
            messages=[{"role": "user", "content": "title"}],
        )

    assert result is response
    assert client.chat.completions.create.call_count == 2
