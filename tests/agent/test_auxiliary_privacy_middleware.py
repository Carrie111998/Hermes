"""Auxiliary provider calls obey the installation's privacy middleware contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import auxiliary_client
from agent.auxiliary_client import CodexAuxiliaryClient, call_llm
from hermes_cli.middleware import RequiredMiddlewareError


def _client(create):
    return SimpleNamespace(
        base_url="https://public.example.invalid/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )


def test_sync_auxiliary_completion_runs_through_llm_middleware(monkeypatch):
    provider_requests = []
    middleware_context = []

    client = _client(
        lambda **kwargs: provider_requests.append(kwargs)
        or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
    )

    def middleware(request, next_call, **context):
        middleware_context.append(context)
        return next_call({**request, "privacy_marker": "redacted"})

    monkeypatch.setattr(
        "hermes_cli.middleware.run_llm_execution_middleware", middleware
    )
    monkeypatch.setenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", "true")

    @auxiliary_client._relay_auxiliary_call
    def run(task):
        auxiliary_client._set_relay_auxiliary_route(
            "openai", "synthetic-model", "chat_completions"
        )
        return auxiliary_client._relay_sync_completion(
            client,
            {"model": "synthetic-model", "messages": []},
            provider="openai",
        )

    run("compression")

    assert provider_requests[0]["privacy_marker"] == "redacted"
    assert middleware_context[0]["required"] is True
    assert middleware_context[0]["call_role"] == "auxiliary:compression"
    assert middleware_context[0]["auxiliary_task"] == "compression"
    assert middleware_context[0]["base_url"] == "https://public.example.invalid/v1"


@pytest.mark.asyncio
async def test_strict_async_auxiliary_completion_blocks_before_provider(monkeypatch):
    provider_calls = []

    async def create(**kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="unsafe"))]
        )

    monkeypatch.setenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", "true")

    @auxiliary_client._relay_auxiliary_call_async
    async def run(task):
        auxiliary_client._set_relay_auxiliary_route(
            "openai", "synthetic-model", "chat_completions"
        )
        return await auxiliary_client._relay_async_completion(
            _client(create),
            {"model": "synthetic-model", "messages": []},
            provider="openai",
        )

    with pytest.raises(RequiredMiddlewareError, match="async auxiliary"):
        await run("title_generation")
    assert provider_calls == []


def test_strict_auxiliary_stream_blocks_before_provider(monkeypatch):
    provider_calls = []
    client = _client(lambda **kwargs: provider_calls.append(kwargs) or iter(()))
    monkeypatch.setenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", "true")

    @auxiliary_client._relay_auxiliary_call
    def run(task):
        auxiliary_client._set_relay_auxiliary_route(
            "openai", "synthetic-model", "chat_completions"
        )
        return auxiliary_client._relay_sync_stream(
            client,
            {"model": "synthetic-model", "messages": [], "stream": True},
            provider="openai",
        )

    with pytest.raises(RequiredMiddlewareError, match="streaming auxiliary"):
        run("moa_aggregator")
    assert provider_calls == []


@pytest.mark.parametrize(
    "middleware_callbacks",
    [[], [lambda request, next_call, **_context: next_call(request)]],
    ids=["missing", "registered"],
)
def test_strict_codex_moa_stream_blocks_before_provider(
    monkeypatch, middleware_callbacks
):
    """The public MoA path must fence its completed-response fast path."""
    provider_calls = []
    completed = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="unsafe"))]
    )
    real_client = SimpleNamespace(
        api_key="test-key",
        base_url="https://public.example.invalid/codex/",
        close=lambda: None,
    )
    client = CodexAuxiliaryClient(real_client, "synthetic-model")

    def create(**kwargs):
        provider_calls.append(kwargs)
        return completed

    monkeypatch.setattr(client.chat.completions, "create", create)
    monkeypatch.setattr(
        auxiliary_client,
        "_get_cached_client",
        lambda *args, **kwargs: (client, "synthetic-model"),
    )
    monkeypatch.setenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", "true")
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: SimpleNamespace(
            _middleware={"llm_execution": middleware_callbacks}
        ),
    )

    with pytest.raises(RequiredMiddlewareError, match="streaming auxiliary"):
        call_llm(
            task="moa_aggregator",
            provider="openai-codex",
            model="synthetic-model",
            messages=[{"role": "user", "content": "synthetic"}],
            stream=True,
        )

    assert provider_calls == []
