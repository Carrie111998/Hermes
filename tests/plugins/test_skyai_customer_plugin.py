from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from plugins.skyai_customer import register
from plugins.skyai_customer import public_tools


SKYAI_TOOL_NAMES = {
    "skyai_catalog_search",
    "skyai_product_detail",
    "skyai_product_slots",
    "skyai_campaign_knowledge",
    "skyai_support_knowledge",
    "skyai_event_log_append",
    "skyai_voice_transfer_to_human",
}


class FakeContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def test_registers_public_safe_skyai_tools() -> None:
    ctx = FakeContext()

    register(ctx)

    names = {tool["name"] for tool in ctx.tools}
    assert names == SKYAI_TOOL_NAMES
    assert {tool["toolset"] for tool in ctx.tools} == {"skyai_customer"}


def test_registered_tool_rejects_non_object_arguments_without_repair() -> None:
    ctx = FakeContext()
    register(ctx)
    handler = next(
        tool["handler"]
        for tool in ctx.tools
        if tool["name"] == "skyai_campaign_knowledge"
    )

    with pytest.raises(ValueError, match="exact object"):
        handler(None)


def test_manifest_is_standalone_opt_in_plugin() -> None:
    manifest = yaml.safe_load(Path("plugins/skyai_customer/plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "skyai-customer"
    assert manifest["kind"] == "standalone"
    assert set(manifest["provides_tools"]) == SKYAI_TOOL_NAMES


def test_plugin_manager_loads_skyai_customer_only_when_enabled(monkeypatch, tmp_path: Path) -> None:
    from hermes_cli import plugins as plugins_mod
    from tools.registry import registry

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["skyai-customer"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    for tool_name in SKYAI_TOOL_NAMES:
        registry._tools.pop(tool_name, None)

    manager = plugins_mod.PluginManager()
    try:
        manager.discover_and_load()

        loaded = manager._plugins.get("skyai-customer")
        assert loaded is not None
        assert loaded.enabled is True
        assert set(loaded.tools_registered) == SKYAI_TOOL_NAMES
        assert {registry._tools[name].toolset for name in SKYAI_TOOL_NAMES} == {"skyai_customer"}
    finally:
        for tool_name in SKYAI_TOOL_NAMES:
            registry._tools.pop(tool_name, None)


def test_registered_tool_handlers_accept_hermes_dispatch_context(monkeypatch, tmp_path: Path) -> None:
    from tools.registry import registry

    ctx = FakeContext()
    register(ctx)
    monkeypatch.setenv("SKYAI_V2_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))

    try:
        for tool in ctx.tools:
            registry.register(
                name=tool["name"],
                schema=tool["schema"],
                handler=tool["handler"],
                toolset=tool["toolset"],
            )
        raw_result = registry.dispatch(
            "skyai_event_log_append",
            {
                "event_type": "product_recommended",
                "properties": {"product_id": 10536},
            },
            task_id="runtime-task",
        )
        assert isinstance(raw_result, str)
        result = json.loads(raw_result)

        assert result["status"] == "ok"
        assert (tmp_path / "events.jsonl").exists()
    finally:
        for tool_name in SKYAI_TOOL_NAMES:
            registry._tools.pop(tool_name, None)


def test_voice_transfer_tool_returns_structured_action() -> None:
    reason = " \tcaller prefers a teammate\n"
    spoken_reply = " \tРазбира се, ще Ви прехвърля към колега.\n"
    result = public_tools.handle_skyai_voice_transfer_to_human(
        reason=reason,
        spoken_reply=spoken_reply,
    )

    assert result["status"] == "ok"
    assert result["voice_action"] == "transfer_to_human"
    assert result["transfer"] == {
        "target": "operator_queue",
        "reason": reason,
    }
    assert result["spoken_reply"] == spoken_reply
    assert result["display_reply"] == spoken_reply


def test_voice_transfer_tool_rejects_wrong_types_and_oversized_fields() -> None:
    assert public_tools.handle_skyai_voice_transfer_to_human(
        reason=7,  # type: ignore[arg-type]
        spoken_reply="Ще Ви прехвърля.",
    ) == {
        "status": "error",
        "error": "reason_must_be_a_string",
    }
    assert public_tools.handle_skyai_voice_transfer_to_human(
        reason="model-authored reason",
        spoken_reply="x" * 221,
    ) == {
        "status": "error",
        "error": "spoken_reply_exceeds_220_characters",
    }
    assert public_tools.handle_skyai_voice_transfer_to_human(
        reason="",
        spoken_reply="Ще Ви прехвърля.",
    ) == {
        "status": "error",
        "error": "reason_must_be_nonempty",
    }
    assert public_tools.handle_skyai_voice_transfer_to_human(
        reason="model-authored reason",
        spoken_reply="",
    ) == {
        "status": "error",
        "error": "spoken_reply_must_be_nonempty",
    }


def test_voice_transfer_schema_requires_model_authored_fields() -> None:
    parameters = public_tools.SKYAI_VOICE_TRANSFER_TO_HUMAN_SCHEMA["parameters"]

    assert parameters["required"] == ["reason", "spoken_reply"]
    assert parameters["additionalProperties"] is False


def test_product_detail_normalizes_public_gift_path(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {
            "data": {
                "name": "Офроуд с ATV",
                "slug": "офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв",
                "price": "88",
                "duration": "90 - 120 минути",
                "configurator": {
                    "additions": [
                        {
                            "options": [
                                {
                                    "label": "- за 1 участник над 18 г. на 1 ATV",
                                    "price": "88",
                                }
                            ]
                        }
                    ]
                },
                "secret": "drop-me",
            }
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(
        product_url="https://skyvision.bg/подарък/офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв/"
    )

    assert result["status"] == "ok"
    assert result["product_path"] == "офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв"
    assert "%D0%BF%D0%BE%D0%B4%D0%B0%D1%80%D1%8A%D0%BA" not in calls[0]
    assert result["detail"]["title"] == "Офроуд с ATV"
    assert result["detail"]["public_url"] == (
        "https://skyvision.bg/подарък/офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв/"
    )
    assert result["detail"]["price_eur"] == "44.99"
    assert result["detail"]["configurator"]["options"] == [
        {
            "label": "- за 1 участник над 18 г. на 1 ATV",
            "price_bgn": "88.00",
            "price_eur": "44.99",
        }
    ]


def test_product_detail_exposes_structured_cancellation_policy_facts(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 8.0):
        return {
            "data": {
                "id": 90879,
                "name": "Полет с балон край София",
                "slug": "летене/полет-с-балон-край-софия",
                "categorySlug": "летене",
                "cancellationPolicy": "Безплатно анулиране до 8 часa преди слота",
                "description": "Стар текст: анулиране не е възможно.",
            }
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(
        product_path="летене/полет-с-балон-край-софия"
    )

    assert result["status"] == "ok"
    detail = result["detail"]
    assert detail["id"] == 90879
    assert detail["title"] == "Полет с балон край София"
    assert detail["category_slug"] == "летене"
    assert detail["slug"] == "летене/полет-с-балон-край-софия"
    assert detail["cancellation_policy"] == "Безплатно анулиране до 8 часa преди слота"
    assert detail["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": "Безплатно анулиране до 8 часa преди слота",
        "status": "free_until_hours_before_slot",
        "hours_before_slot": 8,
    }


def test_product_detail_accepts_php_integer_canbook_and_preserves_cancellation_facts(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 10536,
                "name": "Кану-каяк под наем в Родопите",
                "slug": "водни/кану-каяк-под-наем-в-родопите",
                "cancellationPolicy": "Безплатно анулиране до 8 часa преди слота",
                "canBook": 1,
            }
        },
    )

    result = public_tools.handle_skyai_product_detail(
        product_path="водни/кану-каяк-под-наем-в-родопите"
    )

    assert result["status"] == "ok"
    assert "answer" not in result
    assert "guidance" not in result
    assert "template" not in result
    assert "routing" not in result
    detail = result["detail"]
    assert detail["can_book"] is True
    assert detail["cancellation_policy"] == "Безплатно анулиране до 8 часa преди слота"
    assert detail["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": "Безплатно анулиране до 8 часa преди слота",
        "status": "free_until_hours_before_slot",
        "hours_before_slot": 8,
    }
    assert "answer" not in detail
    assert "guidance" not in detail
    assert "template" not in detail
    assert "routing" not in detail


def test_product_detail_accepts_php_integer_zero_canbook(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 10537,
                "name": "Само с ваучер",
                "slug": "категория/само-с-ваучер",
                "cancellationPolicy": "Безплатно анулиране до 24 часа преди слота",
                "canBook": 0,
            }
        },
    )

    detail = public_tools.handle_skyai_product_detail(
        product_path="категория/само-с-ваучер"
    )["detail"]

    assert detail["can_book"] is False
    assert detail["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": "Безплатно анулиране до 24 часа преди слота",
        "status": "free_until_hours_before_slot",
        "hours_before_slot": 24,
    }


def test_product_detail_accepts_observed_public_php_boolean_siblings_without_losing_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 10540,
                "name": "Кану-каяк под наем в Родопите",
                "slug": "каякинг/кану-каяк-под-наем-в-родопите",
                "cancellationPolicy": "Безплатно анулиране до 8 часa преди слота",
                "canBook": 1,
                "canBuyVoucher": 1,
                "canReceiveBonusProduct": 1,
            }
        },
    )

    detail = public_tools.handle_skyai_product_detail(
        product_path="каякинг/кану-каяк-под-наем-в-родопите"
    )["detail"]

    assert detail["can_book"] is True
    assert detail["can_buy_voucher"] is True
    assert detail["includes_bonus"] is True
    assert detail["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": "Безплатно анулиране до 8 часa преди слота",
        "status": "free_until_hours_before_slot",
        "hours_before_slot": 8,
    }


def test_product_detail_accepts_exact_json_boolean_canbook(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 10538,
                "name": "JSON boolean",
                "slug": "категория/json-boolean",
                "cancellationPolicy": "Няма опция за безплатно анулиране",
                "canBook": True,
            }
        },
    )

    detail = public_tools.handle_skyai_product_detail(
        product_path="категория/json-boolean"
    )["detail"]

    assert detail["can_book"] is True
    assert detail["cancellation_policy"] == "Няма опция за безплатно анулиране"


@pytest.mark.parametrize("bad_canbook", [2, -1, 1.0, 0.0, "1", "true"])
def test_product_detail_rejects_unsupported_numeric_and_string_canbook(monkeypatch, bad_canbook) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 10539,
                "name": "Bad canBook",
                "slug": "категория/bad-canbook",
                "cancellationPolicy": "Безплатно анулиране до 48 часа преди слота",
                "canBook": bad_canbook,
            }
        },
    )

    with pytest.raises(ValueError, match="canBook must be an exact boolean"):
        public_tools.handle_skyai_product_detail(product_path="категория/bad-canbook")


def test_product_detail_exposes_no_free_cancellation_policy(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 8.0):
        return {
            "data": {
                "id": 90881,
                "name": "Екстремно преживяване",
                "slug": "екстремно/екстремно-преживяване",
                "cancellationPolicy": "Няма опция за безплатно анулиране",
            }
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(
        product_path="екстремно/екстремно-преживяване"
    )

    assert result["status"] == "ok"
    assert result["detail"]["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": "Няма опция за безплатно анулиране",
        "status": "no_free_cancellation",
        "hours_before_slot": None,
    }


def test_product_detail_marks_missing_cancellation_policy_unknown(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 8.0):
        return {"data": {"id": 42, "name": "Услуга", "slug": "категория/услуга"}}

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(product_path="категория/услуга")

    assert result["status"] == "ok"
    assert result["detail"]["cancellation_policy"] is None
    assert result["detail"]["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": None,
        "status": "unknown",
        "hours_before_slot": None,
    }


def test_product_detail_returns_bounded_error_when_detail_fetch_fails(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 8.0):
        raise TimeoutError("network timeout")

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(product_path="категория/услуга")

    assert result["status"] == "error"
    assert result["error"] == "product_detail_fetch_failed"
    assert result["product_path"] == "категория/услуга"
    assert result["detail"]["cancellation"] == {
        "source_field": "cancellationPolicy",
        "policy": None,
        "status": "unverified",
        "hours_before_slot": None,
    }


def test_catalog_search_converts_eur_budget_to_public_cache_bgn(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {
            "data": [
                    {
                        "id": 2,
                        "title": "SPA",
                        "price": "186",
                        "oldPrice": "220",
                        "isOnOffer": True,
                        "rating": "4.9",
                    "ratingCount": "27",
                    "ordersCount": "105",
                    "location": "София",
                },
                {"id": 1, "title": "Масаж", "price": "100", "location": "София"},
            ]
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    query = " \tмасаж / София до 100 евро?\n"
    result = public_tools.handle_skyai_catalog_search(
        query=query,
        min_price_eur=80,
        max_price_eur=100,
        limit=2,
    )

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["query"] == query
    parsed = urlsplit(calls[0])
    params = parse_qs(parsed.query, keep_blank_values=True)
    assert params["search"] == [query]
    assert params["size"] == ["2"]
    assert params["minPrice"] == ["156"]
    assert params["maxPrice"] == ["196"]
    assert [item["title"] for item in result["items"]] == ["SPA", "Масаж"]
    assert result["items"][0]["title"] == "SPA"
    assert result["items"][0]["price_eur"] == "95.10"
    assert result["items"][0]["old_price_eur"] == "112.48"
    assert result["items"][0]["rating"] == "4.90"
    assert result["items"][0]["rating_count"] == 27
    assert result["items"][0]["orders_count"] == 105
    assert result["items"][0]["is_on_offer"] is True


def test_catalog_search_does_not_infer_budget_or_fetch_a_second_catalog(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {
            "data": [
                {"id": 1, "name": "Backend first", "price": "500"},
                {"id": 2, "name": "Backend second", "price": "50"},
            ]
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_catalog_search(
        query="Търся масаж за двама в София до 100 евро.",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["filters"]["max_price_eur"] is None
    assert "inferred_from_query" not in result["filters"]
    assert len(calls) == 1
    assert parse_qs(urlsplit(calls[0]).query)["maxPrice"] == ["4000"]
    assert [item["title"] for item in result["items"]] == [
        "Backend first",
        "Backend second",
    ]


def test_catalog_search_rejects_malformed_structured_arguments_without_repair(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call API")),
    )

    assert public_tools.handle_skyai_catalog_search(
        query=7,  # type: ignore[arg-type]
    ) == {"status": "error", "error": "query_must_be_a_string"}
    assert public_tools.handle_skyai_catalog_search(
        min_price_eur="80",  # type: ignore[arg-type]
    ) == {"status": "error", "error": "min_price_eur_must_be_a_number"}
    assert public_tools.handle_skyai_catalog_search(
        min_price_eur=100,
        max_price_eur=80,
    ) == {"status": "error", "error": "min_price_eur_exceeds_max_price_eur"}
    assert public_tools.handle_skyai_catalog_search(
        limit=13,
    ) == {"status": "error", "error": "limit_out_of_range"}


def test_catalog_search_returns_public_facts_without_semantic_context(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": 9,
                    "name": "Вино и СПА пакет Стандарт за двама",
                    "price": "255",
                    "slug": "винен-туризъм/вино-и-спа-пакет-стандарт-за-двама",
                    "categorySlug": "винен-туризъм",
                    "locationName": "Могилово",
                    "locationArea": "Stara Zagora",
                }
            ]
        },
    )

    result = public_tools.handle_skyai_catalog_search(
        query="Подарък за близка приятелка от Сливен.",
        limit=5,
    )

    assert set(result) == {"status", "source", "query", "filters", "count", "items"}
    assert "query_evidence" not in result
    assert "location_context" not in result
    assert "selection_context" not in result
    assert "value_voucher_option" not in result
    assert result["items"][0]["category_slug"] == "винен-туризъм"
    assert result["items"][0]["location"] == "Могилово"
    assert result["items"][0]["location_area"] == "Stara Zagora"
    assert "category_key" not in result["items"][0]
    assert "distance_from_requested_location_km" not in result["items"][0]
    assert "requested_location" not in result["items"][0]


def test_catalog_search_preserves_backend_order_and_duplicate_records(monkeypatch) -> None:
    backend_items = [
        {"id": 3, "name": "Third-ranked by backend", "price": "300"},
        {"id": 1, "name": "First duplicate", "price": "10"},
        {"id": 1, "name": "Second duplicate", "price": "10"},
        {"id": 2, "name": "Outside requested result window", "price": "20"},
    ]
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda *_args, **_kwargs: {"data": backend_items},
    )

    result = public_tools.handle_skyai_catalog_search(
        query="Искам да поръчаме един билет за два дни",
        limit=3,
    )

    assert [item["id"] for item in result["items"]] == [3, 1, 1]
    assert [item["title"] for item in result["items"]] == [
        "Third-ranked by backend",
        "First duplicate",
        "Second duplicate",
    ]


def test_catalog_search_does_not_prune_far_or_childlike_options_in_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": 1,
                    "name": "Луксозен СПА ритуал в Сърница",
                    "price": "160",
                    "locationName": "Сърница",
                },
                {
                    "id": 2,
                    "name": "Детско-юношеско офроуд училище край Сливен",
                    "price": "90",
                    "locationName": "Сливен",
                },
            ]
        },
    )

    result = public_tools.handle_skyai_catalog_search(
        query=(
            "Моля за помощ. Близка приятелка има рожден ден. "
            "Подаръкът да е красив. Тя е спокоен и позитивен човек, 50+, от Сливен."
        ),
        limit=5,
    )

    titles = [item["title"] for item in result["items"]]
    assert "Луксозен СПА ритуал в Сърница" in titles
    assert "Детско-юношеско офроуд училище край Сливен" in titles
    assert "location_context" not in result


def test_campaign_knowledge_returns_public_sales_and_terms_facts() -> None:
    result = public_tools.handle_skyai_campaign_knowledge(
        topic="Клиент пита дали бонусният полет може да е за подарения човек",
        include_terms=True,
    )

    assert result["status"] == "ok"
    assert result["tool_contract"]["purpose"] == "public_facts_only"
    assert result["tool_contract"]["reasoning_owner"] == "hermes"
    assert "готови customer-visible реплики" in result["tool_contract"]["notes"]
    campaign = result["active_campaigns"][0]
    assert campaign["public_url"] == "https://skyvision.bg/campaign/free-panoramic-flight/"
    assert "panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto" in campaign["terms_url"]
    assert "човека, който купува или резервира" in campaign["customer_summary"]
    assert "SkyVision е създаден през 2007 от Емил и Малина." in campaign["brand_story_facts"]
    assert "Летенето остава част от ДНК-то на бранда." in campaign["brand_story_facts"]
    assert any("Емил и Малина все още лично летят" in fact for fact in campaign["brand_story_facts"])
    assert campaign["bonus_owner"]["default"].startswith("човекът")
    assert campaign["bonus_owner"]["is_automatic_for_voucher_recipient"] is False
    assert campaign["bonus_owner"]["transfer_is_manual_exception"] is True
    assert campaign["bonus_owner"]["customer_can_self_transfer"] is False
    assert "акаунта, имейла и данните" in campaign["bonus_owner"]["default_use_scope"]
    assert campaign["bonus_owner"]["manual_exception_approver"] == "Емил Ломлиев"
    assert campaign["campaign_2026_facts"]["public_page"] == "https://skyvision.bg/campaign/free-panoramic-flight/"
    assert campaign["campaign_2026_facts"]["archive_2025_url"] == (
        "https://skyvision.bg/campaign/free-panoramic-flight-2025/"
    )
    assert campaign["campaign_2026_facts"]["validity"] == "12 месеца от датата на покупката"
    assert "независимо къде се изпълнява основната" in (
        campaign["campaign_2026_facts"]["main_service_location_independent"]
    )
    assert "единствено от летище Приморско" in campaign["campaign_2026_facts"]["bonus_execution_location"]
    assert "основната услуга и бонусният полет са различни преживявания" in (
        campaign["campaign_2026_facts"]["location_confusion_answer"]
    )
    gift_linking = campaign["campaign_2026_facts"]["gift_entitlement_profile_linking"]
    assert gift_linking["has_voucher_or_serial_number"] is False
    assert gift_linking["manual_add_by_customer"] is False
    assert gift_linking["customer_can_self_transfer"] is False
    assert "автоматично в профила" in gift_linking["logged_in_order"]
    assert "имейла от поръчката" in gift_linking["guest_or_no_profile_order"]
    assert "същия имейл" in gift_linking["later_profile_with_same_email"]
    assert "не се добавя ръчно" in gift_linking["missing_entitlement_resolution"]
    assert "без предварително купуване на ваучер" in campaign["booknow_nuance"]
    assert "парите ще бъдат възстановени" in campaign["booknow_nuance"]
    assert campaign["bonus_product"]["product_id"] == 95435
    assert campaign["bonus_product"]["availability_tool"] == "skyai_product_slots"
    assert campaign["bonus_product"]["availability_facts"]["slots_tool_product_id"] == 95435
    assert campaign["bonus_product"]["public_url"] == (
        "https://skyvision.bg/подарък/полет-с-жирокоптер/панорамен-полет-над-морето/"
    )
    assert campaign["bonus_product"]["duration"] == "10 мин."
    assert campaign["bonus_product"]["location"] == "Летище Приморско"
    assert "винаги се изпълнява от летище Приморско" in campaign["bonus_product"]["location_note"]
    assert campaign["bonus_product"]["price_eur"] == "0.00"
    reservation = campaign["campaign_2026_facts"]["reservation_process"]
    assert reservation["advance_reservation_required"] is True
    assert reservation["booking_channel"] == "SkyVision profile self-service"
    assert reservation["steps"] == [
        "отворете профила в SkyVision",
        "отворете секция „Ваучери“",
        "натиснете „Резервирай“ срещу бонуса за безплатен панорамен полет",
        "изберете свободен таймслот",
        "завършете онлайн резервацията",
    ]
    assert "след изпълнение на основната услуга" in reservation["booknow_unlock_rule"]
    assert "24 часа" in reservation["reserve_before_timeslot"]
    assert "72 часа" in reservation["self_service_cancel_before_timeslot"]
    assert "лошо време" in reservation["weather_rebooking"]
    assert "избере нов свободен таймслот" in reservation["weather_rebooking"]
    assert reservation["normal_booking_method"] == "profile_vouchers_reserve_button"
    assert reservation["support_escalation_when_missing"] == (
        "ако бонусът, профилната връзка или бутонът „Резервирай“ липсват, насочи към официалната поддръжка за проверка по акаунта без публично искане на пълни идентификатори"
    )
    assert "клиентът пита" in result["founder_transfer_facts"]["context"]
    founder_facts = result["founder_transfer_facts"]["facts"]
    assert founder_facts["default_owner"].startswith("купувачът")
    assert "акаунта/имейла/данните" in founder_facts["default_rule"]
    assert founder_facts["founder_name"] == "Емил Ломлиев"
    assert founder_facts["cofounder_name"] == "Малина Ломлиева"
    assert "през 2007 г." in founder_facts["founding_story"]
    assert "над 1000 преживявания" in founder_facts["platform_scale"]
    assert "все още лично летят" in founder_facts["personal_flight_fact"]
    assert "пилот-инструктор" in founder_facts["founder_role"]
    assert "лично одобрение" in founder_facts["recipient_transfer"]
    assert "public_founder_contact" in founder_facts["recipient_transfer_approval"]
    assert result["founder_transfer_facts"]["public_founder_contact"] == "+359 886 417 142"


def test_support_knowledge_returns_public_commerce_and_voucher_facts() -> None:
    result = public_tools.handle_skyai_support_knowledge(
        topic="Клиент пита как да напише пожелание, как се доставя и как да удължи ваучер",
        include_contacts=True,
    )

    assert result["status"] == "ok"
    assert result["source"] == "skyvision_curated_public_support_knowledge"
    assert "Честитка" in result["gift_voucher_presentation"]["voucher_blanks"]
    wish_flow_text = " ".join(result["gift_voucher_presentation"]["wish_flow"])
    assert "Купи ваучер" in wish_flow_text
    assert "отваря създателя на ваучер" in wish_flow_text
    assert "Име на ползвател" in wish_flow_text
    assert "Поздрав" in wish_flow_text
    assert "веднага" in wish_flow_text
    assert "Редактирай поздрава" in wish_flow_text
    assert "размер на шрифта" in wish_flow_text
    assert "височина на реда" in wish_flow_text
    assert "не е необходим" in wish_flow_text
    assert "Кошница" in wish_flow_text
    assert "молива" in wish_flow_text
    assert "Бланка" in wish_flow_text
    assert "Запази" in wish_flow_text
    assert "Резервирай" in wish_flow_text
    assert "BookNow" in wish_flow_text
    assert "не издава подаръчен ваучер" in wish_flow_text
    assert "няма персонализация на поздрав" in wish_flow_text
    assert "натиска „Редактирай поздрава“, за да се обнови preview-то" not in wish_flow_text
    assert result["gift_voucher_presentation"]["packaging_options"] == [
        {
            "name": "Безплатна опаковка",
            "price_eur": "0.00",
            "price_bgn": "0.00",
            "note": "универсална подаръчна опаковка",
        },
        {
            "name": "Син плик „Лукс“",
            "price_eur": "2.00",
            "price_bgn": "3.91",
            "note": "класическият SkyVision син плик с червен восъчен печат; разпознаваем премиум вариант",
        },
        {
            "name": "Плик с кауза „Пингвин“",
            "price_eur": "5.00",
            "price_bgn": "9.78",
            "note": "подаръчен плик с кауза",
        },
        {
            "name": "Електронен ваучер",
            "price_eur": "0.00",
            "price_bgn": "0.00",
            "note": "най-бързият вариант, когато подаръкът трябва да се изпрати веднага онлайн",
        },
    ]
    serialized = str(result["gift_voucher_presentation"])
    assert "физическа опаковка" not in serialized
    assert "червен восъчен печат" in serialized
    assert result["delivery"]["courier"] == "Speedy"
    assert result["delivery"]["current_fee"] == "безплатна доставка"
    assert result["delivery"]["office_locator_url"] == "https://www.speedy.bg/bg/speedy-offices-automats"
    assert "15:00" in result["delivery"]["dispatch_cutoff"]
    assert "Speedy локатора" in result["delivery"]["speedy_working_hours_fact"]
    assert "EUR е основната цена" in result["gift_voucher_presentation"]["display_facts"]["price_display"]
    assert result["payment_methods"]["online_checkout_options"] == ["Карта", "EasyPay", "Наложен платеж"]
    assert result["payment_methods"]["cash_on_delivery"]["not_for"] == (
        "електронен ваучер и директна BookNow резервация"
    )
    assert result["payment_methods"]["bank_transfer"]["available_in_online_checkout"] is False
    assert result["payment_methods"]["bank_transfer"]["online_checkout_label"] is None
    assert "до 5 дни" in result["order_and_invoice_support"]["invoice_self_service"]
    assert "потвърдителния имейл" in result["order_and_invoice_support"]["invoice_self_service"]
    assert "15:00" in result["order_and_invoice_support"]["next_day_delivery_fact"]
    reservation_contact = result["reservation_support"]
    assert reservation_contact["customer_contact_email"] == "info@skyvision.bg"
    assert reservation_contact["automated_notification_address"] == {
        "email": "reservations@skyvision.bg",
        "role": "автоматични известия за резервации",
        "monitored": True,
        "accepts_customer_replies": False,
    }
    assert "direct_email" not in reservation_contact
    assert "изтекъл" in result["reservation_support"]["expired_response_deadline"]
    assert "удължаване" in " ".join(result["vouchers"]["extension_steps"])
    assert "ако системата я показва" not in " ".join(result["vouchers"]["extension_steps"])
    assert "изтрие ваучера" in result["vouchers"]["manual_extension_refresh"]
    assert "добави отново" in result["vouchers"]["manual_extension_refresh"]
    assert "друг потребител" in result["vouchers"]["ownership_conflict"]
    issuer_scope = result["vouchers"]["issuer_scope"]
    assert issuer_scope["skyvision_issued_vouchers"] == {
        "profile_compatible": True,
        "service_authority": "SkyVision",
    }
    assert issuer_scope["externally_issued_vouchers"]["profile_compatible"] is False
    assert "издал ваучера" in issuer_scope["externally_issued_vouchers"]["service_authority"]
    assert issuer_scope["catalog_overlap_changes_issuer_or_compatibility"] is False
    assert issuer_scope["compatibility_is_issuer_scoped"] is True
    assert "Неуточнен ваучер" in issuer_scope["unspecified_origin_in_skyvision_chat"]
    panel = result["vouchers"]["customer_panel"]
    assert panel["scope"] == "Процесът в SkyVision профила е за ваучери, издадени от SkyVision."
    assert panel["main_area"] == "Профил -> Ваучери / Моите ваучери"
    assert panel["left_navigation"] == ["Ваучери", "Резервации", "Запитвания", "Поръчки", "Настройки", "Изход"]
    assert "Чакащо запитване" in panel["voucher_filters"]
    assert "Добави ваучер" in panel["empty_state"]
    assert "серийния номер на ваучера на латиница" in " ".join(panel["add_voucher_flow"])
    assert panel["add_voucher_flow"][0].startswith("За ваучер, издаден от SkyVision")
    assert "латиница" in panel["addition_problem_facts"]["serial_format"]
    assert panel["addition_problem_facts"]["possible_issuer_mismatch"] is True
    assert panel["voucher_list_columns"] == ["Услуга", "Ваучер", "Депозит", "Статус", "Валидност", "Действия"]
    assert "„Използвай“" in " ".join(panel["common_actions"])
    assert panel["customer_remains_actor"] is True
    assert "не иска пълен код" in panel["chat_privacy_note"]
    campaign_gifts = result["vouchers"]["campaign_gifts"]
    assert "нямат ваучерен/сериен номер" in campaign_gifts["not_regular_vouchers"]
    assert campaign_gifts["manual_add_available"] is False
    assert "автоматично в профила" in campaign_gifts["profile_linking"]["logged_in_order"]
    assert "имейла от поръчката" in campaign_gifts["profile_linking"]["guest_or_no_profile_order"]
    assert "същия имейл" in campaign_gifts["profile_linking"]["later_profile_with_same_email"]
    assert "не го карай да въвежда сериен номер" in campaign_gifts["customer_next_step"]
    assert result["vouchers"]["profile_extension_available"] is True
    assert "остатъчен ваучер" in result["vouchers"]["residual_voucher"]["automatic_issue"]
    assert result["vouchers"]["residual_voucher"]["email_subject"] == "Издаване на остатък за ваучер от SkyVision"
    assert result["vouchers"]["merge_two_vouchers_into_one"]["self_service_available"] is False
    assert result["vouchers"]["merge_two_vouchers_into_one"]["handling"] == "ръчна обработка"
    assert "ръчна" in result["vouchers"]["merge_two_vouchers_into_one"]["handling"]
    assert "1 месец" in result["vouchers"]["merge_two_vouchers_into_one"]["result"]
    assert "грешна опция" in result["vouchers"]["wrong_reservation_or_split_residual_repair"]
    assert "Сторно/рефънд" in result["vouchers"]["refund_and_cancellation"]
    email_learning = result["email_case_learning"]
    assert email_learning["source"] == "customer_safe_email_case_learning_2025-07-07_to_2026-07-07"
    assert email_learning["scale"]["human_operational_case_records"] == 6979
    assert email_learning["scale"]["grouped_cases"] == 3099
    assert email_learning["privacy"].startswith("Обобщено обучение")
    learned_intents = {item["intent"] for item in email_learning["frequent_customer_intents"]}
    assert learned_intents == {
        "checkout_payment_problem",
        "courier_delivery_details",
        "post_experience_media",
        "provider_unreachable",
        "refund_cancellation_return",
        "reservation_status",
        "voucher_help",
    }
    refund_intent = next(
        item for item in email_learning["frequent_customer_intents"] if item["intent"] == "refund_cancellation_return"
    )
    assert "минимален контекст" in refund_intent["support_posture"]
    assert "има/няма резервация" in email_learning["state_reasoning"][0]
    assert "пълен ваучерен код" in email_learning["operator_handoff"]["do_not_request_in_public_chat"]
    assert "номер на ваучер или поръчка" in email_learning["operator_handoff"]["minimum_safe_identifier"]
    assert result["official_contacts"]["contacts_page"] == "https://skyvision.bg/контакти/"
    assert result["official_contacts"]["email"] == "info@skyvision.bg"
    assert "+359 (0) 700 20 200" in result["official_contacts"]["phones"]


def test_product_slots_returns_all_exact_slot_facts_without_mode_classification(
    monkeypatch,
) -> None:
    def fake_http_json(url: str, *, timeout: float = 8.0):
        return {
            "fixedSlots": [
                {
                    "id": 1,
                    "start": "2026-07-06T05:00:00.000000Z",
                    "end": "2026-07-06T05:40:00.000000Z",
                    "slots": [
                        {
                            "id": 10,
                            "status": "free",
                            "start": "2026-07-06T05:00:00.000000Z",
                            "end": "2026-07-06T05:40:00.000000Z",
                        }
                    ],
                }
            ],
            "requestSlots": [{"start": "2026-07-06T08:00:00", "end": "2026-07-06T08:40:00"}],
            "workingPeriods": [],
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_slots(
        product_id=10536,
        start_date="2026-07-03",
        end_date="2026-07-17",
    )

    assert result["status"] == "ok"
    assert "availability_mode" not in result
    assert "mode_facts" not in result
    assert result["fixed_slots"][0]["free_slots_count"] == 1
    assert result["fixed_slots"][0]["first_free_slot"]["id"] == 10
    assert result["request_slots"] == [
        {
            "start": "2026-07-06T08:00:00",
            "end": "2026-07-06T08:40:00",
        }
    ]


def test_product_slots_rejects_type_repairs_and_partial_date_range() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        public_tools.handle_skyai_product_slots(product_id="10536")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provided together"):
        public_tools.handle_skyai_product_slots(
            product_id=10536,
            start_date="2026-07-03",
        )


def test_product_facts_preserve_false_and_do_not_infer_offer(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": [
                {
                    "id": 1,
                    "title": "Exact facts",
                    "price": "100",
                    "oldPrice": "120",
                    "isOnOffer": False,
                }
            ]
        },
    )

    result = public_tools.handle_skyai_catalog_search(query="", limit=1)

    assert result["items"][0]["is_on_offer"] is False


def test_product_detail_uses_field_presence_not_truthy_aliases(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tools,
        "_http_json",
        lambda _url, timeout=8.0: {
            "data": {
                "id": 0,
                "product_id": 999,
                "title": "",
                "name": "must not replace exact empty title",
                "slug": "exact",
                "forKids": False,
                "children": True,
                "includesBonus": False,
                "canReceiveBonusProduct": True,
                "canBook": False,
                "canBuyVoucher": False,
            }
        },
    )

    detail = public_tools.handle_skyai_product_detail(
        product_path="exact",
    )["detail"]

    assert detail["id"] == 0
    assert detail["title"] == ""
    assert detail["for_kids"] is False
    assert detail["includes_bonus"] is False
    assert detail["can_book"] is False
    assert detail["can_buy_voucher"] is False


def test_event_log_append_rejects_properties_outside_positive_schema(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SKYAI_V2_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))

    result = public_tools.handle_skyai_event_log_append(
        event_type="chat_message_customer",
        properties={"email": "client@example.com"},
    )

    assert result["status"] == "error"
    assert result["error"] == "unsupported_event_property"
    assert result["written"] is False
    assert not (tmp_path / "events.jsonl").exists()


def test_event_log_preserves_unregistered_lookalikes_and_redacts_registered_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    registered_secret = "registered-secret-value-123456"
    lookalikes = "client@example.com | +359 888 123 456 | sk-lookalike-only"
    anonymous_id = " \tanon-1\n"
    conversation_id = " conversation-1 "
    monkeypatch.setenv("SKYAI_V2_EVENT_LOG_PATH", str(path))
    monkeypatch.setenv("SKYAI_V2_CANARY_TOKEN", registered_secret)

    result = public_tools.handle_skyai_event_log_append(
        event_type="product_recommended",
        anonymous_id=anonymous_id,
        conversation_id=conversation_id,
        properties={
            "product_id": 10536,
            "surface": lookalikes,
            "reason_code": f"before:{registered_secret}:after",
        },
    )

    assert result["status"] == "ok"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == "skyai_ci.events.local_jsonl.v2"
    assert record["properties"]["surface"] == lookalikes
    assert record["properties"]["reason_code"] == "before:[redacted-secret]:after"
    assert record["anonymous_id_hash"] == hashlib.sha256(anonymous_id.encode("utf-8")).hexdigest()
    assert record["conversation_id_hash"] == hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()


def test_event_log_append_writes_sanitized_append_only_record(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("SKYAI_V2_EVENT_LOG_PATH", str(path))

    result = public_tools.handle_skyai_event_log_append(
        event_type="product_recommended",
        anonymous_id="anon-1",
        conversation_id="conversation-1",
        properties={"product_id": 10536, "surface": "canary"},
    )

    assert result["status"] == "ok"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "product_recommended"
    assert record["anonymous_id_hash"]
    assert record["conversation_id_hash"]
    assert record["properties"] == {"product_id": 10536, "surface": "canary"}
