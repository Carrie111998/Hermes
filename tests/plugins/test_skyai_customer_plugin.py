from __future__ import annotations

import json
from pathlib import Path

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
        result = registry.dispatch(
            "skyai_event_log_append",
            {
                "event_type": "product_recommended",
                "properties": {"product_id": 10536},
            },
            task_id="runtime-task",
        )

        assert result["status"] == "ok"
        assert (tmp_path / "events.jsonl").exists()
    finally:
        for tool_name in SKYAI_TOOL_NAMES:
            registry._tools.pop(tool_name, None)


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


def test_catalog_search_converts_eur_budget_to_public_cache_bgn(monkeypatch) -> None:
    calls: list[str] = []
    public_tools._CATALOG_INDEX_CACHE["items"] = None
    public_tools._CATALOG_INDEX_CACHE["expires_at"] = 0

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {
            "data": [
                {"id": 1, "title": "Масаж", "price": "100", "location": "София"},
                {"id": 2, "title": "SPA", "price": "186", "location": "София"},
            ]
        }

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_catalog_search(
        query="масаж София",
        min_price_eur=80,
        max_price_eur=100,
        limit=1,
    )

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert "search=%D0%BC%D0%B0%D1%81%D0%B0%D0%B6%20%D0%A1%D0%BE%D1%84%D0%B8%D1%8F" in calls[0]
    assert "minPrice=156" in calls[0]
    assert "maxPrice=196" in calls[0]
    assert result["items"][0]["title"] == "SPA"
    assert result["items"][0]["price_eur"] == "95.10"


def test_catalog_search_falls_back_to_daily_index_and_reranks(monkeypatch) -> None:
    calls: list[str] = []
    public_tools._CATALOG_INDEX_CACHE["items"] = None
    public_tools._CATALOG_INDEX_CACHE["expires_at"] = 0

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        if url.endswith("search="):
            return {
                "data": [
                    {
                        "id": 1,
                        "name": "Йога клас с малки кученца в София",
                        "price": "45",
                        "slug": "приключения-с-домашни-любимци/йога-клас-с-малки-кученца-софия",
                        "locationName": "София",
                    },
                    {
                        "id": 2,
                        "name": "Уелнес ритуал за двама: Сауна и масаж",
                        "price": "195.583",
                        "slug": "релакс-зона/сауна-и-масаж-за-двама",
                        "locationName": "София",
                    },
                    {
                        "id": 3,
                        "name": "Кралски синхронен масаж за двойки или приятели",
                        "price": "130",
                        "slug": "масажи/кралски-синхронен-масаж-за-двойки-или-приятели",
                        "locationName": "София",
                    },
                    {
                        "id": 4,
                        "name": "Какаов синхронен масаж за двама – гръб или цяло тяло",
                        "price": "120",
                        "slug": "масажи/какаов-синхронен-масаж-за-двама-цяло-тяло",
                        "locationName": "София",
                    },
                    {
                        "id": 5,
                        "name": "Сладкарски курс за Италиански десерти",
                        "price": "115.39",
                        "slug": "сладкарски-курс/италиански-десерти",
                        "locationName": "София-град",
                    },
                ]
            }
        return {"data": []}

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_catalog_search(
        query="Търся масаж за двама в София до 100 евро.",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["filters"]["max_price_eur"] == 100.0
    assert result["filters"]["inferred_from_query"]["max_price_eur"] is True
    assert len(calls) == 2
    assert calls[1].endswith("search=")
    assert [item["title"] for item in result["items"]] == [
        "Уелнес ритуал за двама: Сауна и масаж",
        "Кралски синхронен масаж за двойки или приятели",
        "Какаов синхронен масаж за двама – гръб или цяло тяло",
    ]
    assert all("/подарък/" in item["public_url"] for item in result["items"])


def test_catalog_ranking_diversifies_broad_discovery_without_hurting_specific_queries() -> None:
    ranked = [
        {"id": 1, "title": "Офроуд разходка с АТВ до Пловдив", "category_slug": "офроуд-атв-под-наем"},
        {"id": 2, "title": "Офроуд с АТВ 200 CC в района на Пловдив", "category_slug": "офроуд-атв-под-наем"},
        {"id": 3, "title": "Самостоятелен бънджи скок от балон - Пловдив", "category_slug": "скок-с-бънджи"},
        {"id": 4, "title": "Дегустация на вино за двама в Пловдив", "category_slug": "винени-турове-дегустации"},
    ]

    broad = public_tools._diversify_ranked_products(
        ranked,
        tokens=["подарък", "мъж", "пловдив"],
    )
    specific = public_tools._diversify_ranked_products(
        ranked,
        tokens=["атв", "пловдив"],
    )

    assert [item["id"] for item in broad[:3]] == [1, 3, 4]
    assert [item["id"] for item in specific[:2]] == [1, 2]


def test_campaign_knowledge_returns_public_sales_and_terms_guidance() -> None:
    result = public_tools.handle_skyai_campaign_knowledge(
        topic="Клиент пита дали бонусният полет може да е за подарения човек",
        include_terms=True,
    )

    assert result["status"] == "ok"
    campaign = result["active_campaigns"][0]
    assert campaign["public_url"] == "https://skyvision.bg/campaign/free-panoramic-flight/"
    assert "panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto" in campaign["terms_url"]
    assert campaign["bonus_product"]["product_id"] == 95435
    assert campaign["bonus_product"]["availability_tool"] == "skyai_product_slots"
    assert result["founder_transfer_guidance"]["use_only_when_customer_asks_to_transfer_bonus_flight"] is True
    founder_summary = result["founder_transfer_guidance"]["summary"]
    assert "съосновател" in founder_summary
    assert "пилот-инструктор" in founder_summary
    assert result["founder_transfer_guidance"]["public_founder_contact"] == "+359 886 417 142"


def test_support_knowledge_returns_public_commerce_and_voucher_guidance() -> None:
    result = public_tools.handle_skyai_support_knowledge(
        topic="Клиент пита как да напише пожелание, как се доставя и как да удължи ваучер",
        include_contacts=True,
    )

    assert result["status"] == "ok"
    assert result["source"] == "skyvision_curated_public_support_knowledge"
    assert "Честитка" in result["gift_voucher_presentation"]["voucher_blanks"]
    assert "Редактирай поздрава" in " ".join(result["gift_voucher_presentation"]["wish_flow"])
    assert result["gift_voucher_presentation"]["packaging_options"] == [
        {
            "name": "Безплатна опаковка",
            "price_eur": "0.00",
            "price_bgn": "0.00",
            "note": "универсална физическа опаковка",
        },
        {
            "name": "Син плик Лукс",
            "price_eur": "2.00",
            "price_bgn": "3.91",
            "note": "по-официален и премиум вид",
        },
        {
            "name": "Плик с кауза „Пингвин“",
            "price_eur": "5.00",
            "price_bgn": "9.78",
            "note": "физически плик с кауза",
        },
        {
            "name": "Електронен ваучер",
            "price_eur": "0.00",
            "price_bgn": "0.00",
            "note": "най-бързият вариант; не е физическа опаковка",
        },
    ]
    assert result["delivery"]["courier"] == "Speedy"
    assert result["delivery"]["current_fee"] == "безплатна доставка"
    assert result["delivery"]["office_locator_url"] == "https://www.speedy.bg/bg/speedy-offices-automats"
    assert "EUR първо" in result["gift_voucher_presentation"]["answer_guidance"]
    assert result["payment_methods"]["online_checkout_options"] == ["Карта", "EasyPay", "Наложен платеж"]
    assert result["payment_methods"]["bank_transfer"]["available_in_online_checkout"] is False
    assert result["payment_methods"]["bank_transfer"]["answer_only_if_asked"] is True
    assert "удължаване" in " ".join(result["vouchers"]["extension_flow"])
    assert "двата ваучера" in " ".join(result["vouchers"]["combine_or_use_multiple_vouchers_flow"])
    assert result["official_contacts"]["contacts_page"] == "https://skyvision.bg/контакти/"
    assert result["official_contacts"]["email"] == "info@skyvision.bg"
    assert "+359 (0) 700 20 200" in result["official_contacts"]["phones"]


def test_product_slots_compacts_fixed_slots_and_marks_fixed_mode(monkeypatch) -> None:
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
    assert result["availability_mode"] == "fixed_slots_available_direct_booking"
    assert result["fixed_slots"][0]["free_slots_count"] == 1
    assert result["fixed_slots"][0]["first_free_slot"]["id"] == 10


def test_event_log_append_rejects_sensitive_payload(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SKYAI_V2_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))

    result = public_tools.handle_skyai_event_log_append(
        event_type="chat_message_customer",
        properties={"email": "client@example.com"},
    )

    assert result["status"] == "blocked"
    assert result["written"] is False
    assert not (tmp_path / "events.jsonl").exists()


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
