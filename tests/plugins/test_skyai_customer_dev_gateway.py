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
    assert ("GET", "/ready") in routes
    assert ("GET", "/version") in routes
    assert ("GET", "/widget/chatkit/") in routes
    assert ("POST", "/chatkit/dev-message") in routes
    assert ("POST", "/chatkit/message") in routes
    assert ("POST", "/qa/compare") in routes


def test_render_widget_html_contains_fab_compatible_chat_endpoint(tmp_path: Path) -> None:
    html = dev_gateway.render_widget_html(settings(tmp_path, version="test-version"))

    assert "<title>SkyAI v2 DEV Canary</title>" in html
    assert "test-version" in html
    assert "fetch('/chatkit/dev-message'" in html
    assert "skyai-v2-canary-conversation-id" in html


def test_system_prompt_links_campaign_bonus_id_to_slots_tool() -> None:
    prompt = dev_gateway.build_skyai_system_prompt()

    assert "bonus_product.product_id" in prompt
    assert "skyai_product_slots" in prompt
    assert "skyai_support_knowledge" in prompt


def test_build_cards_from_reply_enriches_visible_product_links(monkeypatch) -> None:
    seen = {}

    def fake_detail(product_url="", product_path=""):
        seen["product_url"] = product_url
        return {
            "status": "ok",
            "detail": {
                "title": "Полет с жирокоптер MTO-Sport",
                "public_url": "https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/",
                "price_eur": "101.75",
                "price_bgn": "199.00",
                "location": "Приморско",
                "images": [{"src": "https://cdn.example/gyro.jpg"}],
            },
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    cards = dev_gateway.build_cards_from_reply(
        "Виж [този полет](https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/). "
        "Кампанията е тук: https://skyvision.bg/campaign/free-panoramic-flight/"
    )

    assert seen["product_url"].startswith("https://skyvision.bg/подарък/полет-с-жирокоптер/")
    assert cards == [
        {
            "title": "Полет с жирокоптер MTO-Sport",
            "public_url": "https://skyvision.bg/подарък/полет-с-жирокоптер/полет-с-жирокоптер-mto-sport/",
            "price_eur": "101.75",
            "price_bgn": "199.00",
            "location": "Приморско",
            "image": "https://cdn.example/gyro.jpg",
        }
    ]


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


def test_format_discord_mirror_message_uses_customer_visible_shape() -> None:
    message = dev_gateway.format_discord_mirror_message(
        {"conversation_id": "c1", "message": "Търся подарък"},
        {
            "status": "ok",
            "version": "v-test",
            "conversation_id": "c1",
            "reply": "Имаме чудесни идеи.",
            "trace": {
                "runtime": "hermes_agent",
                "toolset": "skyai_customer",
                "live_model": True,
                "fallback": False,
                "latency_ms": 12,
            },
        },
    )

    assert "**Клиент**" in message
    assert "**SkyAI**" in message
    assert "Търся подарък" in message
    assert "Имаме чудесни идеи." in message
    assert "toolset=skyai_customer" in message


@pytest.mark.asyncio
async def test_mirror_to_discord_skips_when_disabled(tmp_path: Path) -> None:
    result = await dev_gateway.mirror_to_discord(
        {"message": "Здравей"},
        {"status": "ok", "reply": "Здравей", "trace": {}},
        settings(tmp_path),
    )

    assert result == {"status": "skipped", "reason": "disabled"}


@pytest.mark.asyncio
async def test_build_compare_response_runs_dev_and_prod_sides(tmp_path: Path) -> None:
    async def fake_runner(message, history, conversation_id, canary_settings):
        return f"DEV: {message}"

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": f"PROD: {payload['message']}",
            "cards": [{"title": "card"}],
            "trace": {"model": "gpt-5.5", "latency_ms": 20},
        }

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["status"] == "ok"
    assert response["dev_v2"]["reply"] == "DEV: Има ли масаж?"
    assert response["prod_current"]["reply"] == "PROD: Има ли масаж?"
    assert response["prod_current"]["cards_count"] == 1
    assert response["cards_compare"]["prod_count"] == 1


@pytest.mark.asyncio
async def test_build_compare_response_compares_card_links_prices_and_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_detail(product_url="", product_path=""):
        return {
            "status": "ok",
            "detail": {
                "title": "Масаж за двама",
                "public_url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                "price_eur": "90.00",
                "location": "София",
                "images": [{"src": "https://cdn.example/massage.jpg"}],
            },
        }

    async def fake_runner(message, history, conversation_id, canary_settings):
        return "Бих предложил https://skyvision.bg/подарък/масаж/масаж-за-двама/"

    def fake_prod_caller(payload, canary_settings):
        return {
            "status": "ok",
            "version": "prod-v",
            "reply": "PROD reply",
            "cards": [
                {
                    "title": "Масаж за двама",
                    "url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
                    "price_eur": "90.00",
                    "image_url": "https://cdn.example/massage.jpg",
                }
            ],
            "trace": {"model": "gpt-5.5"},
        }

    monkeypatch.setattr(dev_gateway.public_tools, "handle_skyai_product_detail", fake_detail)

    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Има ли масаж?"},
        settings(tmp_path, compare_prod_base_url="https://prod.example"),
        agent_runner=fake_runner,
        prod_caller=fake_prod_caller,
    )

    assert response["dev_v2"]["cards"] == [
        {
            "title": "Масаж за двама",
            "public_url": "https://skyvision.bg/подарък/масаж/масаж-за-двама/",
            "price_eur": "90.00",
            "location": "София",
            "image": "https://cdn.example/massage.jpg",
        }
    ]
    assert response["cards_compare"]["shared_urls"] == [
        "https://skyvision.bg/подарък/масаж/масаж-за-двама"
    ]
    assert response["cards_compare"]["shared_titles"] == ["масаж за двама"]
    assert response["cards_compare"]["dev_missing_price_count"] == 0
    assert response["cards_compare"]["prod_missing_image_count"] == 0


@pytest.mark.asyncio
async def test_build_compare_response_requires_prod_base_url(tmp_path: Path) -> None:
    response = await dev_gateway.build_compare_response(
        {"conversation_id": "c1", "message": "Здравей"},
        settings(tmp_path),
    )

    assert response["status"] == "error"
    assert response["error"] == "compare_prod_not_configured"
