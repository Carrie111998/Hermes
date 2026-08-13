"""Tests for resolve_provider_client's ``custom`` + ``explicit_base_url`` branch
when the endpoint speaks Anthropic Messages.

When the main provider is ``custom`` and its ``base_url`` ends in ``/anthropic``
(a proxied Anthropic gateway — MiniMax, Zhipu GLM, LiteLLM, or a self-hosted
LLM proxy), auxiliary tasks reach ``resolve_provider_client("custom",
explicit_base_url=..., api_mode="anthropic_messages")`` — directly for a
per-task ``auxiliary.<task>`` override, or via ``_resolve_auto`` Step 1 which
forwards the main runtime's ``api_mode``.

The bug (issue #16254): this branch called ``_to_openai_base_url()``
unconditionally, stripping the ``/anthropic`` tail to ``/v1`` even for
``api_mode=anthropic_messages``.  The Anthropic wrapper then never saw the real
``/anthropic`` path, so every side task (title generation, compression, vision,
web_extract, session_search) hit ``.../v1/chat/completions`` on a Messages-only
endpoint and failed.  The sibling named-custom-provider branch already guarded
the rewrite on ``api_mode``; this makes the explicit-base branch consistent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


_ANTHROPIC_BASE = "https://gateway.example.com/proxy/anthropic"


def _client_base_url(client) -> str:
    for chain in (("base_url",), ("_real_client", "base_url"), ("_client", "base_url")):
        obj = client
        try:
            for attr in chain:
                obj = getattr(obj, attr)
            return str(obj)
        except AttributeError:
            continue
    return ""


def test_explicit_base_anthropic_messages_keeps_anthropic_path():
    """api_mode=anthropic_messages must build the Anthropic wrapper on the raw
    ``/anthropic`` base — not the ``/v1``-rewritten one."""
    from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient

    fake_anthropic = MagicMock(name="anthropic_sdk_client")
    with patch(
        "agent.anthropic_adapter.build_anthropic_client",
        return_value=fake_anthropic,
    ) as mock_build:
        client, model = resolve_provider_client(
            "custom",
            model="claude-opus-4-8",
            explicit_base_url=_ANTHROPIC_BASE,
            explicit_api_key="k",
            api_mode="anthropic_messages",
        )

    assert isinstance(client, AnthropicAuxiliaryClient), (
        "custom endpoint with api_mode=anthropic_messages must return the native "
        f"Anthropic wrapper, got {type(client).__name__}"
    )
    # The wrapper — and the Anthropic SDK client it was built from — must keep
    # the /anthropic path, NOT the /v1-rewritten one.
    mock_build.assert_called_once_with("k", _ANTHROPIC_BASE)
    assert client.base_url == _ANTHROPIC_BASE
    assert model == "claude-opus-4-8"


def test_explicit_base_anthropic_messages_openai_fallback_uses_v1():
    """When the anthropic SDK is unavailable, _maybe_wrap_anthropic returns the
    plain OpenAI client — which must be on the /v1 base, never /anthropic."""
    from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient

    with patch(
        "agent.anthropic_adapter.build_anthropic_client",
        side_effect=ImportError("anthropic package not installed"),
    ):
        client, model = resolve_provider_client(
            "custom",
            model="claude-opus-4-8",
            explicit_base_url=_ANTHROPIC_BASE,
            explicit_api_key="k",
            api_mode="anthropic_messages",
        )

    assert client is not None
    assert not isinstance(client, AnthropicAuxiliaryClient)
    # /anthropic → /v1 so the OpenAI SDK never hits /anthropic/chat/completions.
    assert _client_base_url(client).rstrip("/").endswith("/proxy/v1")


def test_explicit_base_without_anthropic_mode_preserves_v1_rewrite():
    """Regression: with no anthropic_messages api_mode, the /anthropic → /v1
    OpenAI-wire rewrite is preserved (fix is scoped, no behavior change)."""
    from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient

    client, model = resolve_provider_client(
        "custom",
        model="my-model",
        explicit_base_url=_ANTHROPIC_BASE,
        explicit_api_key="k",
        api_mode="chat_completions",
    )

    assert client is not None
    assert not isinstance(client, AnthropicAuxiliaryClient)
    assert _client_base_url(client).rstrip("/").endswith("/proxy/v1")


def test_api_key_provider_wrap_uses_explicit_override_not_raw_creds_base():
    """Regression for issue #85535: the generic API-key branch (a built-in
    named provider like "deepseek" whose fallback_model entry overrides its
    endpoint via explicit_base_url) must make the Anthropic-wrap decision
    against the SAME effective base_url the OpenAI client was actually
    built with, not a stale pre-rewrite value.

    Uses an api.kimi.com/coding override, which _to_openai_base_url()
    rewrites by APPENDING "/v1" (unlike the /anthropic-suffix case,
    which STRIPS a marker -- entangled with the separate, not-yet-landed
    #85532 rewrite-policy fix). Both the pre-rewrite raw_base_url
    ("https://api.kimi.com/coding") and the post-rewrite base_url used to
    build the actual client ("https://api.kimi.com/coding/v1") match the
    "api.kimi.com/coding" wrap heuristic, so the wrap itself doesn't
    distinguish the bug -- but the EXACT URL string passed to
    build_anthropic_client does. Before the fix, it received the raw,
    /v1-less URL: inconsistent with the endpoint the OpenAI client was
    actually pointed at."""
    import model_tools  # noqa: F401 -- registers built-in provider profiles
    from agent.auxiliary_client import resolve_provider_client, AnthropicAuxiliaryClient

    override_base = "https://api.kimi.com/coding"
    effective_base = "https://api.kimi.com/coding/v1"  # after _to_openai_base_url()
    fake_anthropic = MagicMock(name="anthropic_sdk_client")
    with patch(
        "agent.anthropic_adapter.build_anthropic_client",
        return_value=fake_anthropic,
    ) as mock_build:
        client, model = resolve_provider_client(
            "deepseek",
            model="some-model",
            explicit_base_url=override_base,
            explicit_api_key="k",
        )

    assert isinstance(client, AnthropicAuxiliaryClient), (
        "an explicit_base_url override pointing at an Anthropic-wire "
        f"endpoint must be wrapped, got {type(client).__name__}"
    )
    # The wrap must be built against the SAME effective URL the OpenAI
    # client was actually constructed with -- not the stale pre-rewrite
    # value -- so the Anthropic SDK client targets the same endpoint.
    mock_build.assert_called_once_with("k", effective_base)
