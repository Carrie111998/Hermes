from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import skyai_v2_compare_matrix as matrix


def test_load_scenarios_preserves_authored_text_exactly(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": " x ",
                    "message": " \tЗдравей\n",
                    "focus": " exact ",
                    "history": [{"role": "user", "content": " преди "}],
                }
            ]
        ),
        encoding="utf-8",
    )

    assert matrix.load_scenarios(path) == [
        {
            "id": " x ",
            "message": " \tЗдравей\n",
            "focus": " exact ",
            "history": [{"role": "user", "content": " преди "}],
        }
    ]


@pytest.mark.parametrize(
    "scenario",
    [
        {"id": 1, "message": "x"},
        {"id": "x", "message": 1},
        {"id": "x", "message": "y", "focus": []},
        {"id": "x", "message": "y", "history": {}},
    ],
)
def test_load_scenarios_rejects_malformed_fields_exactly(
    tmp_path: Path,
    scenario: dict[str, object],
) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps([scenario]), encoding="utf-8")

    with pytest.raises(ValueError):
        matrix.load_scenarios(path)


def test_build_compare_payload_is_stable_and_preserves_message() -> None:
    payload = matrix.build_compare_payload(
        {
            "id": "massage",
            "message": " \tТърся масаж\n",
            "history": [],
        },
        run_id="run1",
    )

    assert payload == {
        "conversation_id": "skyai-v2-compare-run1-massage",
        "message": " \tТърся масаж\n",
        "surface": "skyai_v2_compare_matrix",
    }


def test_build_compare_payload_rejects_overlong_id_instead_of_truncating() -> None:
    with pytest.raises(
        ValueError,
        match="compare_conversation_id_exceeds_256_bytes",
    ):
        matrix.build_compare_payload(
            {"id": "x" * 300, "message": "message", "history": []},
            run_id="run1",
        )


def test_run_matrix_uses_injected_caller_and_reports_raw_facts_only() -> None:
    calls = []

    def fake_caller(base_url, payload, timeout, bearer_token):
        calls.append((base_url, payload, timeout, bearer_token))
        return {
            "status": "ok",
            "dev_v2": {
                "status": "ok",
                "reply": " DEV reply\n",
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
        "dev_cards": 1,
        "prod_cards": 2,
        "shared_urls": ["https://skyvision.bg/подарък/a"],
        "only_dev_urls": [],
        "only_prod_urls": ["https://skyvision.bg/подарък/b"],
        "dev_missing_price_count": 0,
        "prod_missing_price_count": 0,
        "dev_missing_image_count": 0,
        "prod_missing_image_count": 1,
        "dev_reply_preview": " DEV reply\n",
        "prod_reply_preview": "PROD reply",
    }
    assert "quality_score" not in json.dumps(
        report["results"][0]["summary"],
        ensure_ascii=False,
    )
    assert not hasattr(matrix, "evaluate_side")


def test_render_console_summary_contains_only_mechanical_counts() -> None:
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
    assert "case1: status=ok dev_cards=1 prod_cards=2 shared_urls=1" in rendered
    assert "score" not in rendered
    assert "issues" not in rendered


def test_preview_does_not_casefold_or_rewrite_authored_text() -> None:
    assert matrix._preview(" \tДа,\nExact  Text ") == " \tДа,\nExact  Text "
