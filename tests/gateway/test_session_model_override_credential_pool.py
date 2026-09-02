"""Session /model overrides must attach credential_pool for 402 rotation."""

from __future__ import annotations

from unittest.mock import MagicMock

from gateway.run import GatewayRunner, _credential_pool_for_provider


def test_fast_session_override_includes_credential_pool(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "sess-1": {
            "model": "kimi-k2.7",
            "provider": "custom:hyper",
            "api_key": "sk-test",
            "base_url": "https://hyper.charm.land/v1",
            "api_mode": "chat_completions",
        },
    }
    fake_pool = object()

    monkeypatch.setattr(
        "gateway.run._resolve_gateway_model",
        lambda _uc=None: "default-model",
    )
    monkeypatch.setattr(
        "gateway.run._credential_pool_for_provider",
        lambda provider: fake_pool if provider == "custom:hyper" else None,
    )

    model, runtime = runner._resolve_session_agent_runtime(session_key="sess-1")

    assert model == "kimi-k2.7"
    assert runtime.get("credential_pool") is fake_pool


def test_fast_session_override_carries_configured_default_route(monkeypatch):
    runner = object.__new__(GatewayRunner)
    override = {
        "model": "override-model",
        "provider": "openrouter",
        "api_key": "sk-test",
    }
    runner._session_model_overrides = {"sess-1": override}
    monkeypatch.setattr(
        "gateway.run._credential_pool_for_provider",
        lambda _provider: None,
    )

    model, runtime = runner._resolve_session_agent_runtime(
        session_key="sess-1",
        user_config={
            "model": {
                "default": "model-a",
                "provider": "commandcode",
                "base_url": "https://commandcode.ai/v1",
            }
        },
    )

    assert model == override["model"]
    assert runtime["_configured_default_route"] == {
        "provider": "commandcode",
        "model": "model-a",
        "base_url": "https://commandcode.ai/v1",
    }
    route = runner._resolve_turn_agent_config("hello", model, runtime)
    assert route["configured_default_route"] == runtime["_configured_default_route"]
    assert "_configured_default_route" not in route["runtime"]


def test_fast_session_override_infers_default_provider_for_legacy_configs(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openai",
            "requested_provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        "gateway.run._credential_pool_for_provider",
        lambda _provider: None,
    )

    for model_config in ("global-model", {"default": "global-model"}):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {
            "sess-1": {
                "model": "override-model",
                "provider": "openrouter",
                "api_key": "sk-test",
            }
        }

        model, runtime = runner._resolve_session_agent_runtime(
            session_key="sess-1",
            user_config={"model": model_config},
        )

        assert model == "override-model"
        assert runtime["_configured_default_route"] == {
            "provider": "openai",
            "model": "global-model",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
        }


def test_fast_session_override_survives_unresolvable_default(monkeypatch):
    def fail_default(**_kwargs):
        raise RuntimeError("default credentials unavailable")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fail_default,
    )
    monkeypatch.setattr(
        "gateway.run._credential_pool_for_provider",
        lambda _provider: None,
    )
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "sess-1": {
            "model": "override-model",
            "provider": "openrouter",
            "api_key": "sk-test",
        }
    }

    model, runtime = runner._resolve_session_agent_runtime(
        session_key="sess-1",
        user_config={"model": "global-model"},
    )

    assert model == "override-model"
    assert "_configured_default_route" not in runtime


def test_equal_model_auth_fallback_does_not_supply_provider_for_legacy_default(
    monkeypatch,
):
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {
        "sess-1": {
            "model": "override-model",
            "provider": "openrouter",
        }
    }
    fallback_runtime = {
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "model": "global-model",
        "api_key": "fallback-key",
        "base_url": "https://openrouter.ai/api/v1",
    }
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: fallback_runtime.copy(),
    )
    runner._apply_session_model_override = (
        lambda _session_key, _model, runtime: ("override-model", runtime)
    )

    model, runtime = runner._resolve_session_agent_runtime(
        session_key="sess-1",
        user_config={"model": "global-model"},
    )

    assert model == "override-model"
    assert runtime["provider"] == "openrouter"
    assert "_configured_default_route" not in runtime

