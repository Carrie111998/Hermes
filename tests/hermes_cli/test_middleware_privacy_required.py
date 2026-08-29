"""Fail-closed contract for installations that require LLM middleware."""

from __future__ import annotations

import types

import pytest

from hermes_cli.middleware import (
    RequiredMiddlewareError,
    llm_execution_middleware_required,
    run_llm_execution_middleware,
)


def _manager(callbacks):
    return types.SimpleNamespace(_middleware={"llm_execution": callbacks})


def test_required_execution_blocks_when_no_middleware_is_registered(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([])
    )
    provider_calls = []

    with pytest.raises(RequiredMiddlewareError, match="required.*not registered"):
        run_llm_execution_middleware(
            {"messages": [{"role": "user", "content": "synthetic"}]},
            lambda request: provider_calls.append(request),
            required=True,
        )

    assert provider_calls == []


def test_required_execution_blocks_when_middleware_raises_before_provider(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("privacy plugin crashed")

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([broken])
    )
    provider_calls = []

    with pytest.raises(RequiredMiddlewareError, match="failed before provider"):
        run_llm_execution_middleware(
            {"messages": [{"role": "user", "content": "synthetic"}]},
            lambda request: provider_calls.append(request),
            required=True,
        )

    assert provider_calls == []


def test_required_execution_allows_registered_middleware_to_call_provider(monkeypatch):
    def privacy_middleware(request, next_call, **_kwargs):
        return next_call({**request, "redacted": True})

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: _manager([privacy_middleware]),
    )

    result = run_llm_execution_middleware(
        {"messages": []},
        lambda request: request,
        required=True,
    )
    assert result["redacted"] is True


def test_optional_execution_preserves_upstream_no_plugin_behavior(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _manager([])
    )
    request = {"messages": []}
    assert run_llm_execution_middleware(request, lambda value: value) is request


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_required_mode_environment_switch(monkeypatch, value):
    monkeypatch.setenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", value)
    assert llm_execution_middleware_required() is True


def test_required_mode_defaults_off_for_upstream_compatibility(monkeypatch):
    monkeypatch.delenv("HERMES_REQUIRE_LLM_EXECUTION_MIDDLEWARE", raising=False)
    assert llm_execution_middleware_required() is False
