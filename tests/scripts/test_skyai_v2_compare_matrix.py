from __future__ import annotations

import json
from pathlib import Path

from scripts import skyai_v2_compare_matrix as matrix


def test_load_scenarios_validates_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps([{"id": "x", "message": "Здравей"}]), encoding="utf-8")

    assert matrix.load_scenarios(path) == [
        {"id": "x", "message": "Здравей", "focus": "", "history": []}
    ]


def test_build_compare_payload_is_stable_and_fab_style() -> None:
    payload = matrix.build_compare_payload(
        {"id": "massage", "message": "Търся масаж", "history": []},
        run_id="run1",
    )

    assert payload == {
        "conversation_id": "skyai-v2-compare-run1-massage",
        "message": "Търся масаж",
        "surface": "skyai_v2_compare_matrix",
    }


def test_run_matrix_uses_injected_caller_and_summarizes_cards() -> None:
    calls = []

    def fake_caller(base_url, payload, timeout, bearer_token):
        calls.append((base_url, payload, timeout, bearer_token))
        return {
            "status": "ok",
            "dev_v2": {
                "status": "ok",
                "reply": "DEV reply",
                "cards_count": 1,
            },
            "prod_current": {
                "status": "ok",
                "reply": "PROD reply",
                "cards_count": 2,
            },
            "cards_compare": {
                "shared_urls": ["https://skyvision.bg/подарък/a"],
                "only_dev_urls": [],
                "only_prod_urls": ["https://skyvision.bg/подарък/b"],
                "dev_missing_price_count": 0,
                "prod_missing_price_count": 0,
                "dev_missing_image_count": 0,
                "prod_missing_image_count": 1,
            },
        }

    report = matrix.run_matrix(
        [{"id": "case1", "message": "Въпрос", "focus": "cards"}],
        base_url="https://dev.example",
        timeout=12,
        bearer_token="token",
        caller=fake_caller,
        run_id="run1",
    )

    assert calls[0][0] == "https://dev.example"
    assert calls[0][1]["conversation_id"] == "skyai-v2-compare-run1-case1"
    assert calls[0][2] == 12
    assert calls[0][3] == "token"
    assert report["results"][0]["summary"] == {
        "id": "case1",
        "focus": "cards",
        "status": "ok",
        "dev_status": "ok",
        "prod_status": "ok",
        "dev_quality_score": 100,
        "prod_quality_score": 100,
        "dev_quality_issues": [],
        "prod_quality_issues": [],
        "dev_cards": 1,
        "prod_cards": 2,
        "shared_urls": ["https://skyvision.bg/подарък/a"],
        "only_dev_urls": [],
        "only_prod_urls": ["https://skyvision.bg/подарък/b"],
        "dev_missing_price_count": 0,
        "prod_missing_price_count": 0,
        "dev_missing_image_count": 0,
        "prod_missing_image_count": 1,
        "dev_reply_preview": "DEV reply",
        "prod_reply_preview": "PROD reply",
    }


def test_render_console_summary_contains_core_counts() -> None:
    report = {
        "scenario_count": 1,
        "base_url": "https://dev.example",
        "results": [
            {
                "summary": {
                    "id": "case1",
                    "status": "ok",
                    "dev_cards": 1,
                    "prod_cards": 2,
                    "dev_quality_score": 80,
                    "prod_quality_score": 100,
                    "dev_quality_issues": ["example_issue"],
                    "prod_quality_issues": [],
                    "shared_urls": ["x"],
                    "focus": "cards",
                    "dev_reply_preview": "DEV",
                    "prod_reply_preview": "PROD",
                }
            }
        ],
    }

    rendered = matrix.render_console_summary(report)

    assert "SkyAI v2 compare matrix: 1 scenarios" in rendered
    assert "case1: status=ok dev_cards=1 prod_cards=2 shared_urls=1 dev_score=80 prod_score=100" in rendered
    assert "dev issues: example_issue" in rendered


def test_evaluate_bonus_transfer_requires_default_buyer_owner() -> None:
    scenario = {"id": "bonus_transfer_customer"}
    side = {
        "status": "ok",
        "reply": "Да, идеята е бонусът да може да зарадва и човека, за когото купувате подаръка.",
        "cards": [],
    }

    result = matrix.evaluate_side(scenario, side)

    assert result["score"] == 40
    assert result["issues"] == [
        "starts_with_direct_yes_on_exception_case",
        "missing_default_bonus_owner",
        "implies_automatic_recipient_bonus",
    ]


def test_evaluate_booknow_refund_language_prefers_will_be_refunded() -> None:
    scenario = {"id": "booknow_bonus_use_timing"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": "При лошо време парите може да бъдат възстановени.",
            "cards": [],
        },
    )

    assert "weak_or_missing_booknow_refund_language" in result["issues"]
    assert "weak_booknow_refund_may_language" in result["issues"]


def test_evaluate_voucher_merge_rejects_self_service_merge_flow() -> None:
    scenario = {"id": "voucher_merge"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": (
                "Можете да използвате два ваучера за едно преживяване, като добавите "
                "двата ваучера в профила и после изберете Имам ваучер. Ако не стане, екипът помага ръчно."
            ),
            "cards": [],
        },
    )

    assert "suggests_self_service_merge_flow" in result["issues"]


def test_evaluate_voucher_extend_rejects_conditional_availability() -> None:
    scenario = {"id": "voucher_extend"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": "Отвори ваучера и използвай опцията за удължаване, ако системата я показва.",
            "cards": [],
        },
    )

    assert "weak_or_conditional_extension_availability" in result["issues"]


def test_evaluate_gift_wish_rejects_recipient_name_warning_template() -> None:
    scenario = {"id": "gift_wish_text"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": (
                "Пожеланието се пише в Поздрав и после натискаш Редактирай поздрава. "
                "Само да не го объркаш с Име на ползвател."
            ),
            "cards": [],
        },
    )

    assert "unnecessary_recipient_name_field_warning" in result["issues"]


def test_evaluate_calm_sliven_rejects_far_locations_before_nearby() -> None:
    scenario = {"id": "calm_friend_50_sliven"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": "Бих предложил вариант в Сърница и София.",
            "cards": [
                {"title": "СПА в Сърница", "location": "Сърница", "public_url": "https://skyvision.bg/подарък/spa/a/"},
                {"title": "Арт преживяване", "location": "София", "public_url": "https://skyvision.bg/подарък/art/b/"},
            ],
        },
    )

    assert "jumps_far_from_sliven_before_nearby_options" in result["issues"]


def test_evaluate_broad_gift_diversity_uses_cards_only_in_qa_script() -> None:
    scenario = {"id": "broad_gift_diverse"}

    result = matrix.evaluate_side(
        scenario,
        {
            "status": "ok",
            "reply": "Ето три идеи.",
            "cards": [
                {"title": "ATV 1", "public_url": "https://skyvision.bg/подарък/офроуд/a/"},
                {"title": "ATV 2", "public_url": "https://skyvision.bg/подарък/офроуд/b/"},
                {"title": "ATV 3", "public_url": "https://skyvision.bg/подарък/офроуд/c/"},
            ],
        },
    )

    assert result["issues"] == ["low_card_category_diversity"]
