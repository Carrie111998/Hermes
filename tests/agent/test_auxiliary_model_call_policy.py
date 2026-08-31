"""Auxiliary model calls share the fail-closed provider-attempt gate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.auxiliary_client import call_llm
from hermes_cli.model_call_policy import ModelCallPolicyDenied


def test_auxiliary_deny_makes_zero_provider_calls(monkeypatch):
    create = MagicMock(name="provider_create")
    client = SimpleNamespace(
        base_url="https://provider.example/v1?credential=hidden",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    policy_payload = {}

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: (
            "custom",
            "configured-model",
            "https://provider.example/v1",
            "secret",
            "chat_completions",
        ),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        lambda *_args, **_kwargs: (client, "configured-model"),
    )
    monkeypatch.setattr(
        "hermes_cli.lifecycle.has_hook",
        lambda name: name == "pre_model_call_policy",
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "slice-1")
    monkeypatch.setenv("HERMES_KANBAN_MISSION", "mission-root")

    def deny(_name, **payload):
        policy_payload.update(payload)
        return [{"action": "deny", "message": "mission budget exhausted"}]

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", deny)

    with pytest.raises(ModelCallPolicyDenied, match="mission budget exhausted"):
        call_llm(
            task="title_generation",
            provider="custom",
            model="configured-model",
            messages=[{"role": "user", "content": "title this"}],
        )

    create.assert_not_called()
    assert policy_payload["call_kind"] == "auxiliary"
    assert policy_payload["task_id"] == "slice-1"
    assert policy_payload["mission_id"] == "mission-root"
    assert policy_payload["auxiliary_task"] == "title_generation"
    assert policy_payload["provider"] == "custom"
    assert policy_payload["model"] == "configured-model"
    assert policy_payload["base_url_host"] == "provider.example"
    assert "credential" not in str(policy_payload)


@pytest.mark.asyncio
async def test_async_auxiliary_deny_makes_zero_provider_calls(monkeypatch):
    create = AsyncMock(name="provider_create")
    client = SimpleNamespace(
        base_url="https://async-provider.example/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: (
            "custom",
            "async-model",
            "https://async-provider.example/v1",
            "secret",
            "chat_completions",
        ),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        lambda *_args, **_kwargs: (client, "async-model"),
    )
    monkeypatch.setattr(
        "hermes_cli.lifecycle.has_hook",
        lambda name: name == "pre_model_call_policy",
    )
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda _name, **_payload: [
            {"action": "deny", "message": "async budget exhausted"}
        ],
    )

    from agent.auxiliary_client import async_call_llm

    with pytest.raises(ModelCallPolicyDenied, match="async budget exhausted"):
        await async_call_llm(
            task="compression",
            provider="custom",
            model="async-model",
            messages=[{"role": "user", "content": "compress"}],
        )

    create.assert_not_awaited()
