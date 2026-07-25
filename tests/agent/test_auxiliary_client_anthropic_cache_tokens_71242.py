"""Regression test for issue #71242.

``_AnthropicCompletionsAdapter.create`` rebuilds the native Anthropic
usage object into an OpenAI-shaped ``SimpleNamespace``.  Before this fix
the cache accounting fields (``cache_read_input_tokens`` /
``cache_creation_input_tokens``) were dropped, so downstream billing /
usage accounting could not see prompt-cache reads or creations.

This test exercises the usage-mapping path directly by mocking
``create_anthropic_message`` (so no SDK is required) and the transport's
``normalize_response`` (which only needs to return a duck-typed object
with the attributes the adapter reads: ``content``, ``tool_calls``,
``reasoning``, ``finish_reason``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_native_response(*, input_tokens=10, output_tokens=20,
                           cache_read=0, cache_creation=0):
    """Build a duck-typed Anthropic response with a native usage block."""
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # Anthropic does NOT set total_tokens; the adapter computes it.
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    return SimpleNamespace(
        content=[MagicMock()],
        usage=usage,
        stop_reason="end_turn",
        model="claude-test",
    )


def _patch_transport():
    """Patch the anthropic_messages transport so no SDK import is needed."""
    fake_nr = SimpleNamespace(
        content="hello",
        tool_calls=None,
        reasoning=None,
        finish_reason="stop",
    )
    fake_transport = MagicMock(name="anthropic_transport")
    fake_transport.normalize_response.return_value = fake_nr
    return patch(
        "agent.transports.get_transport",
        return_value=fake_transport,
    ), fake_nr


def _patch_create_anthropic_message(response):
    return patch(
        "agent.anthropic_adapter.create_anthropic_message",
        return_value=response,
    )


def test_anthropic_adapter_preserves_cache_tokens_in_usage():
    """Issue #71242: cache_read/cache_creation tokens must survive the shim."""
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    native = _make_native_response(
        input_tokens=100, output_tokens=50,
        cache_read=2048, cache_creation=512,
    )

    adapter = _AnthropicCompletionsAdapter(
        real_client=MagicMock(), model="claude-test", is_oauth=False,
    )

    with _patch_create_anthropic_message(native), _patch_transport()[0]:
        result = adapter.create(messages=[{"role": "user", "content": "hi"}])

    assert result.usage is not None, "usage must be populated"
    # Core OpenAI-shape fields preserved.
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 50
    assert result.usage.total_tokens == 150
    # The regression: cache fields must NOT be dropped.
    assert result.usage.cache_read_input_tokens == 2048, (
        "cache_read_input_tokens was dropped by the usage shim (issue #71242)"
    )
    assert result.usage.cache_creation_input_tokens == 512, (
        "cache_creation_input_tokens was dropped by the usage shim (issue #71242)"
    )


def test_anthropic_adapter_usage_cache_fields_default_to_zero_when_absent():
    """When the provider omits cache fields, the shim must default to 0."""
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    # Native usage WITHOUT the cache fields (some providers don't report them).
    native = SimpleNamespace(
        content=[MagicMock()],
        usage=SimpleNamespace(input_tokens=5, output_tokens=7),
        stop_reason="end_turn",
        model="claude-test",
    )

    adapter = _AnthropicCompletionsAdapter(
        real_client=MagicMock(), model="claude-test", is_oauth=False,
    )

    with _patch_create_anthropic_message(native), _patch_transport()[0]:
        result = adapter.create(messages=[{"role": "user", "content": "hi"}])

    assert result.usage is not None
    assert result.usage.prompt_tokens == 5
    assert result.usage.completion_tokens == 7
    # getattr(..., 0) fallback must yield 0, not raise AttributeError.
    assert result.usage.cache_read_input_tokens == 0
    assert result.usage.cache_creation_input_tokens == 0