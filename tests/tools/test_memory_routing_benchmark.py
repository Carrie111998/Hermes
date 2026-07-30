from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tools.memory_routing_benchmark import (
    assert_isolated_hermes_home,
    compute_metrics,
    load_fixture,
    main,
    score_answer,
)


def test_assert_isolated_hermes_home_rejects_real_home_and_descendants() -> None:
    real_home = Path.home() / ".hermes"

    with pytest.raises(ValueError, match="isolated"):
        assert_isolated_hermes_home(real_home)

    with pytest.raises(ValueError, match="isolated"):
        assert_isolated_hermes_home(real_home / "profiles" / "default")


def test_assert_isolated_hermes_home_accepts_temp_like_external_path(tmp_path: Path) -> None:
    safe_home = tmp_path / "hermes-home"

    assert assert_isolated_hermes_home(safe_home) == safe_home.resolve()


def test_score_answer_uses_conservative_substring_claims_for_ko_and_en() -> None:
    expected = {
        "judged_claims": [
            {"lang": "ko", "must_satisfy": ["파란색 머그", "핸드드립"], "must_not_satisfy": ["라떼"]},
            {"lang": "en", "must_satisfy": ["blue mug"], "must_not_satisfy": ["latte"]},
        ]
    }

    passed = score_answer("파란색 머그에 핸드드립 coffee; blue mug preference.", expected)
    failed_missing = score_answer("파란색 머그만 언급", expected)
    failed_forbidden = score_answer("파란색 머그 핸드드립 blue mug latte", expected)

    assert passed["passed"] is True
    assert passed["satisfied"] == 3
    assert passed["violations"] == []
    assert failed_missing["passed"] is False
    assert "missing:핸드드립" in failed_missing["violations"]
    assert failed_forbidden["passed"] is False
    assert "forbidden:latte" in failed_forbidden["violations"]


def test_compute_metrics_groups_accuracy_and_guardrail_rates() -> None:
    results = [
        {
            "id": "direct-global-pass",
            "task_type": "direct_recall",
            "memory_location": "global",
            "split": "known_domain",
            "control": False,
            "score": {"passed": True},
            "routing_useful": False,
            "personalized": True,
            "safety_miss": False,
        },
        {
            "id": "direct-global-fail",
            "task_type": "direct_recall",
            "memory_location": "global",
            "split": "known_domain",
            "control": False,
            "score": {"passed": False},
            "routing_useful": False,
            "personalized": False,
            "safety_miss": False,
        },
        {
            "id": "indirect-project-pass",
            "task_type": "indirect_application",
            "memory_location": "project",
            "split": "known_domain",
            "control": False,
            "score": {"passed": True},
            "routing_useful": False,
            "personalized": True,
            "safety_miss": True,
        },
        {
            "id": "unseen-routing-useful",
            "task_type": "indirect_application",
            "memory_location": "skill",
            "split": "unseen_topic",
            "control": False,
            "score": {"passed": True},
            "routing_useful": True,
            "personalized": True,
            "safety_miss": False,
        },
        {
            "id": "control-personalized",
            "task_type": "direct_recall",
            "memory_location": "none",
            "split": "known_domain",
            "control": True,
            "score": {"passed": False},
            "routing_useful": False,
            "personalized": True,
            "safety_miss": False,
        },
    ]

    metrics = compute_metrics(results)

    assert metrics["direct_recall_accuracy"]["overall"] == pytest.approx(1 / 3)
    assert metrics["direct_recall_accuracy"]["by_memory_location"]["global"] == pytest.approx(1 / 2)
    assert metrics["direct_recall_accuracy"]["by_memory_location"]["none"] == pytest.approx(0.0)
    assert metrics["direct_recall_accuracy"]["by_split"]["known_domain"] == pytest.approx(1 / 3)
    assert metrics["indirect_application_accuracy"]["overall"] == pytest.approx(1.0)
    assert metrics["indirect_application_accuracy"]["by_memory_location"]["project"] == pytest.approx(1.0)
    assert metrics["indirect_application_accuracy"]["by_split"]["unseen_topic"] == pytest.approx(1.0)
    assert metrics["unseen_topic_useful_routing_rate"] == pytest.approx(1.0)
    assert metrics["false_positive_personalization_rate"] == pytest.approx(1.0)
    assert metrics["safety_miss_rate"] == pytest.approx(1 / 5)


def test_inmind_mini_fixture_has_mvp_coverage() -> None:
    fixture_path = Path("tests/fixtures/memory_routing/inmind_mini.yaml")
    cases = load_fixture(fixture_path)

    assert len(cases) >= 10
    assert {case["split"] for case in cases} >= {"known_domain", "unseen_topic"}
    assert {case["control"] for case in cases} == {False, True}
    assert {case["memory_location"] for case in cases} >= {"global", "project", "skill", "none"}
    langs = {claim["lang"] for case in cases for claim in case["expected"]["judged_claims"]}
    assert langs >= {"ko", "en"}


def test_cli_scorer_only_emits_metrics_without_p0_claims(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_path = tmp_path / "results.jsonl"
    rows = [
        {
            "id": "case-1",
            "task_type": "direct_recall",
            "memory_location": "global",
            "split": "known_domain",
            "control": False,
            "answer": "Use the blue mug.",
            "expected": {"judged_claims": [{"lang": "en", "must_satisfy": ["blue mug"]}]},
        }
    ]
    results_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    exit_code = main(["score", "--mode", "baseline", "--results", str(results_path)])

    captured = capsys.readouterr()
    payload = yaml.safe_load(captured.out)
    assert exit_code == 0
    assert payload["mode"] == "baseline"
    assert payload["scorer_only"] is True
    assert payload["p0_metrics_claimed"] is False
    assert payload["metrics"]["direct_recall_accuracy"]["overall"] == 1.0


def test_cli_accepts_routed_and_open_set_modes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_path = tmp_path / "results.jsonl"
    results_path.write_text("", encoding="utf-8")

    assert main(["score", "--mode", "routed", "--results", str(results_path)]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["mode"] == "routed"
    assert main(["score", "--mode", "open-set", "--results", str(results_path)]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["mode"] == "open-set"
