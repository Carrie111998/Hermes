"""Cron fallback boundaries must constrain auxiliary provider recovery too."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import auxiliary_client as auxiliary


def _install_boundary(policy, chain=None):
    return auxiliary.set_auxiliary_fallback_boundary(policy, chain or [])


def test_boundary_context_is_scoped_and_restored():
    assert auxiliary.get_auxiliary_fallback_boundary() is None

    token = _install_boundary("none")
    try:
        boundary = auxiliary.get_auxiliary_fallback_boundary()
        assert boundary == {"policy": "none", "chain": []}
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)

    assert auxiliary.get_auxiliary_fallback_boundary() is None


def test_none_boundary_blocks_every_auxiliary_alternate_lane(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError(
            "strict none boundary must stop before alternate discovery"
        )

    monkeypatch.setattr(auxiliary, "_get_auxiliary_task_config", unexpected)
    monkeypatch.setattr(auxiliary, "_get_provider_chain", unexpected)
    monkeypatch.setattr(auxiliary, "_read_main_provider", lambda: "")
    monkeypatch.setattr(auxiliary, "_read_main_model", lambda: "")

    from hermes_cli import config as config_module

    monkeypatch.setattr(config_module, "load_config_readonly", unexpected)

    token = _install_boundary("none")
    try:
        assert auxiliary._try_configured_fallback_chain("compression", "primary") == (
            None,
            None,
            "",
        )
        assert auxiliary._try_main_fallback_chain("compression", "primary") == (
            None,
            None,
            "",
        )
        assert auxiliary._try_payment_fallback("primary", "compression") == (
            None,
            None,
            "",
        )
        assert auxiliary._resolve_auto_route(
            main_runtime={"provider": "", "model": ""},
            task="compression",
        ) == (None, None, "")
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)


def test_pinned_boundary_uses_supplied_chain_without_global_read(monkeypatch):
    entry = {"provider": "pinned-provider", "model": "pinned-model"}
    client = MagicMock(name="pinned-client")
    seen = []

    from hermes_cli import config as config_module

    def unexpected_config_read():
        raise AssertionError(
            "pinned auxiliary fallback must not reload the global chain"
        )

    def resolve(candidate):
        seen.append(candidate)
        return client, "pinned-model"

    monkeypatch.setattr(config_module, "load_config_readonly", unexpected_config_read)
    monkeypatch.setattr(auxiliary, "_resolve_fallback_entry", resolve)
    monkeypatch.setattr(auxiliary, "_task_minimum_context_length", lambda _task: None)
    monkeypatch.setattr(auxiliary, "_read_main_provider", lambda: "primary")

    token = _install_boundary("pinned", [entry])
    try:
        assert auxiliary._try_main_fallback_chain("compression", "primary") == (
            client,
            "pinned-model",
            "pinned-provider",
        )
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)

    assert seen == [entry]


def test_none_boundary_blocks_main_agent_model_safety_net(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("none boundary must not inspect the main-agent route")

    monkeypatch.setattr(auxiliary, "_read_main_provider", unexpected)
    monkeypatch.setattr(auxiliary, "_read_main_model", unexpected)

    token = _install_boundary("none")
    try:
        assert auxiliary._try_main_agent_model_fallback(
            "explicit-aux", "compression"
        ) == (None, None, "")
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)


def test_pinned_boundary_routes_explicit_aux_recovery_through_pinned_chain(
    monkeypatch,
):
    client = MagicMock(name="pinned-explicit-recovery")

    monkeypatch.setattr(
        auxiliary,
        "_try_main_fallback_chain",
        lambda *args, **kwargs: (client, "pinned-model", "pinned-provider"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_main_agent_model_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pinned boundary must not use main-agent safety net")
        ),
    )

    token = _install_boundary(
        "pinned", [{"provider": "pinned-provider", "model": "pinned-model"}]
    )
    try:
        assert auxiliary._try_explicit_auxiliary_fallback(
            "compression",
            "explicit-aux",
            reason="connection error",
            failed_model="aux-model",
        ) == (client, "pinned-model", "pinned-provider")
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)


def test_pinned_stale_candidate_recovery_reuses_pinned_chain(monkeypatch):
    client = MagicMock(name="next-pinned-candidate")

    monkeypatch.setattr(
        auxiliary,
        "_try_main_fallback_chain",
        lambda *args, **kwargs: (client, "next-model", "next-provider"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_payment_fallback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pinned boundary must not enter built-in discovery")
        ),
    )

    token = _install_boundary(
        "pinned", [{"provider": "next-provider", "model": "next-model"}]
    )
    try:
        assert auxiliary._try_stale_fallback_recovery(
            "explicit-aux", "compression"
        ) == (client, "next-model", "next-provider")
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)


def test_pinned_unavailable_explicit_primary_uses_authorized_chain_sync(monkeypatch):
    fallback_client = MagicMock(name="pinned-unavailable-sync")
    fallback_client.base_url = "https://approved.example/v1"
    fallback_client.chat.completions.create.return_value = object()

    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_configured_fallback_for_unavailable_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict boundary must not read task-configured fallbacks")
        ),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_main_fallback_chain",
        lambda *_args, **_kwargs: (
            fallback_client,
            "approved-model",
            "approved-provider",
        ),
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda _response, *_args, **_kwargs: "approved-result",
    )

    token = _install_boundary(
        "pinned", [{"provider": "approved-provider", "model": "approved-model"}]
    )
    try:
        result = auxiliary.call_llm(
            task="compression",
            provider="explicit-provider",
            model="explicit-model",
            messages=[{"role": "user", "content": "compress"}],
        )
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)

    assert result == "approved-result"
    fallback_client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_pinned_unavailable_explicit_primary_uses_authorized_chain_async(
    monkeypatch,
):
    fallback_client = MagicMock(name="pinned-unavailable-sync-source")
    async_client = MagicMock(name="pinned-unavailable-async")
    async_client.base_url = "https://approved.example/v1"
    async_client.chat.completions.create = AsyncMock(return_value=object())

    monkeypatch.setattr(
        auxiliary,
        "_get_cached_client",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_configured_fallback_for_unavailable_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict boundary must not read task-configured fallbacks")
        ),
    )
    monkeypatch.setattr(
        auxiliary,
        "_try_main_fallback_chain",
        lambda *_args, **_kwargs: (
            fallback_client,
            "approved-model",
            "approved-provider",
        ),
    )
    monkeypatch.setattr(
        auxiliary,
        "_to_async_client",
        lambda *_args, **_kwargs: (async_client, "approved-model"),
    )
    monkeypatch.setattr(
        auxiliary,
        "_validate_llm_response",
        lambda _response, *_args, **_kwargs: "approved-result",
    )

    token = _install_boundary(
        "pinned", [{"provider": "approved-provider", "model": "approved-model"}]
    )
    try:
        result = await auxiliary.async_call_llm(
            task="compression",
            provider="explicit-provider",
            model="explicit-model",
            messages=[{"role": "user", "content": "compress"}],
        )
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)

    assert result == "approved-result"
    async_client.chat.completions.create.assert_awaited_once()


def test_inherit_boundary_retains_global_main_fallback_behavior(monkeypatch):
    entry = {"provider": "global-provider", "model": "global-model"}
    client = MagicMock(name="global-client")

    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {"fallback_providers": [entry]},
    )
    monkeypatch.setattr(
        auxiliary,
        "_resolve_fallback_entry",
        lambda candidate: (client, candidate["model"]),
    )
    monkeypatch.setattr(auxiliary, "_task_minimum_context_length", lambda _task: None)
    monkeypatch.setattr(auxiliary, "_read_main_provider", lambda: "primary")

    token = _install_boundary("inherit")
    try:
        assert auxiliary._try_main_fallback_chain("compression", "primary") == (
            client,
            "global-model",
            "global-provider",
        )
    finally:
        auxiliary.reset_auxiliary_fallback_boundary(token)
