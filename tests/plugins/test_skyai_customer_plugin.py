from __future__ import annotations

import json
from pathlib import Path

from plugins.skyai_customer import register
from plugins.skyai_customer import public_tools


class FakeContext:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def test_registers_public_safe_skyai_tools() -> None:
    ctx = FakeContext()

    register(ctx)

    names = {tool["name"] for tool in ctx.tools}
    assert names == {
        "skyai_catalog_search",
        "skyai_product_detail",
        "skyai_product_slots",
        "skyai_event_log_append",
    }
    assert {tool["toolset"] for tool in ctx.tools} == {"skyai_customer"}


def test_product_detail_normalizes_public_gift_path(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {"data": {"title": "Офроуд с ATV", "secret": "drop-me"}}

    monkeypatch.setattr(public_tools, "_http_json", fake_http_json)

    result = public_tools.handle_skyai_product_detail(
        product_url="https://skyvision.bg/подарък/офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв/"
    )

    assert result["status"] == "ok"
    assert result["product_path"] == "офроуд-атв-под-наем/семейна-офроуд-разходка-с-атв"
    assert "%D0%BF%D0%BE%D0%B4%D0%B0%D1%80%D1%8A%D0%BA" not in calls[0]
    assert result["detail"] == {"title": "Офроуд с ATV"}


def test_catalog_search_converts_eur_budget_to_public_cache_bgn(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, timeout: float = 8.0):
        calls.append(url)
        return {
            "data": [
                {"id": 1, "title": "Масаж", "priceEur": "90", "location": "София"},
                {"id": 2, "title": "SPA", "priceEur": "95", "location": "София"},
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
