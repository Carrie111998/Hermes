from __future__ import annotations

from pathlib import Path

import pytest

from plugins.skyai_customer import dev_gateway


def settings(tmp_path: Path, **overrides) -> dev_gateway.CanarySettings:
    values = {"profile_home": tmp_path / "profiles" / "skyai-v2-dev"}
    values.update(overrides)
    return dev_gateway.CanarySettings(**values)


def test_validate_settings_allows_loopback_without_token(tmp_path: Path) -> None:
    dev_gateway.validate_settings(settings(tmp_path))


def test_validate_settings_blocks_public_bind_without_explicit_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        dev_gateway.validate_settings(settings(tmp_path, host="0.0.0.0"))


def test_validate_settings_requires_token_for_public_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bearer token"):
        dev_gateway.validate_settings(
            settings(tmp_path, host="0.0.0.0", allow_public_bind=True)
        )


def test_validate_settings_allows_private_bind_with_explicit_gate_without_token(
    tmp_path: Path,
) -> None:
    dev_gateway.validate_settings(
        settings(tmp_path, host="10.80.0.3", allow_public_bind=True)
    )


def test_extract_message_accepts_fab_style_payload() -> None:
    payload = {
        "conversation_id": "abc",
        "history": [{"role": "assistant", "content": "Здравей"}],
        "message": "Искам ваучер за двама",
    }

    assert dev_gateway.extract_message(payload) == "Искам ваучер за двама"


def test_extract_message_falls_back_to_last_customer_message() -> None:
    payload = {
        "messages": [
            {"role": "assistant", "content": "Здравей"},
            {"role": "customer", "content": "Имате ли свободни слотове?"},
        ]
    }

    assert dev_gateway.extract_message(payload) == "Имате ли свободни слотове?"


def test_extract_history_normalizes_customer_role_and_limits() -> None:
    payload = {
        "history": [
            {"role": "system", "content": "drop"},
            {"role": "customer", "content": "Първо"},
            {"role": "assistant", "content": "Второ"},
        ]
    }

    assert dev_gateway.extract_history(payload) == [
        {"role": "user", "content": "Първо"},
        {"role": "assistant", "content": "Второ"},
    ]


@pytest.mark.asyncio
async def test_build_chat_response_dry_run_returns_fab_compatible_shape(tmp_path: Path) -> None:
    response = await dev_gateway.build_chat_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "ok"
    assert response["conversation_id"] == "c1"
    assert response["cards"] == []
    assert response["trace"]["runtime"] == "hermes_agent"
    assert response["trace"]["toolset"] == "skyai_customer"
    assert response["trace"]["live_model"] is False
    assert "dry-run" in response["reply"]


@pytest.mark.asyncio
async def test_build_chat_response_allows_injected_runner(tmp_path: Path) -> None:
    seen = {}

    async def fake_runner(message, history, conversation_id, canary_settings):
        seen.update(
            {
                "message": message,
                "history": history,
                "conversation_id": conversation_id,
                "profile_home": canary_settings.profile_home,
            }
        )
        return "Отговор от тестов runner"

    response = await dev_gateway.build_chat_response(
        {
            "session_id": "thread-1",
            "message": "Покажи ми подарък",
            "history": [{"role": "customer", "content": "Здравей"}],
        },
        settings(tmp_path, live_model=True),
        agent_runner=fake_runner,
    )

    assert response["status"] == "ok"
    assert response["reply"] == "Отговор от тестов runner"
    assert seen["message"] == "Покажи ми подарък"
    assert seen["history"] == [{"role": "user", "content": "Здравей"}]
    assert seen["conversation_id"] == "thread-1"
    assert seen["profile_home"] == tmp_path / "profiles" / "skyai-v2-dev"


def test_create_app_registers_dev_routes(tmp_path: Path) -> None:
    app = dev_gateway.create_app(settings(tmp_path))
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

    assert ("GET", "/health") in routes
    assert ("GET", "/version") in routes
    assert ("POST", "/chatkit/dev-message") in routes
    assert ("POST", "/chatkit/message") in routes


def test_resolve_profile_runtime_reads_model_dict() -> None:
    runtime = dev_gateway._resolve_profile_runtime(
        {
            "model": {
                "default": "gpt-5.5",
                "provider": "openai-codex",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
            }
        }
    )

    assert runtime == {
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "",
    }


def test_resolve_agent_runtime_refreshes_codex_credentials() -> None:
    seen = {}

    def fake_codex_resolver(**kwargs):
        seen.update(kwargs)
        return {
            "api_key": "fresh-oauth-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }

    runtime = dev_gateway._resolve_agent_runtime(
        {
            "model": {
                "default": "gpt-5.5",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            }
        },
        codex_credential_resolver=fake_codex_resolver,
    )

    assert seen == {"refresh_if_expiring": True}
    assert runtime == {
        "model": "gpt-5.5",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
        "api_key": "fresh-oauth-token",
    }


def test_sanitize_runtime_error_redacts_token_markers() -> None:
    assert dev_gateway.sanitize_runtime_error(
        RuntimeError("Bearer abc123 access_token=secret refresh_token:secret2 api_key=secret3")
    ) == "Bearer [redacted] access_token=[redacted] refresh_token=[redacted] api_key=[redacted]"
