from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.turn_router_eval import evaluate_turn_router, load_eval_corpus


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "fixtures" / "turn_router_eval.json"
CLASSIFIER_CORPUS = ROOT / "tests" / "fixtures" / "turn_router_classifier_eval.json"


def test_observe_eval_uses_production_engine_without_application_or_unsafe_escalation():
    report = evaluate_turn_router(load_eval_corpus(CORPUS))

    assert report["contract"] == {
        "mode": "observe",
        "production_decision_engine": "agent.turn_router.decide_turn_route",
        "classifier": "disabled-not-tested",
        "provider_dispatch": False,
        "route_application": False,
        "live_auto": False,
    }
    assert report["metrics"]["total"] >= 15
    assert report["metrics"]["accuracy"] == 1.0
    assert report["metrics"]["false_positives"] == 0
    assert report["metrics"]["false_negatives"] == 0
    assert report["metrics"]["unsafe_escalations"] == 0
    assert {row["language"] for row in report["rows"]} == {"en", "zh"}
    assert all(row["should_apply"] is False for row in report["rows"])
    assert all(not row["unsafe_escalation"] for row in report["rows"] if row["hostile"])


def test_eval_rejects_non_observe_configuration():
    with pytest.raises(ValueError, match="routing.mode=observe"):
        evaluate_turn_router([], config={"mode": "auto"})


def test_shadow_eval_distinguishes_tested_simulated_and_not_tested_lanes():
    report = evaluate_turn_router(
        load_eval_corpus(CORPUS),
        classifier_corpus=load_eval_corpus(CLASSIFIER_CORPUS),
    )

    assert report["contract"]["coverage"] == {
        "tested": ["deterministic_observe_policy"],
        "simulated": [
            "classifier_adapter_schema_timeout_and_injection",
            "session_affinity_and_failure_fail_off",
        ],
        "not_tested": [
            "cache_domain_isolation",
            "live_auto",
            "live_observe_rollout",
            "provider_dispatch",
            "remote_classifier_latency",
            "route_application",
        ],
    }
    classifier = report["classifier_simulation"]
    assert classifier["metrics"]["total"] == 8
    assert classifier["metrics"]["accuracy"] == 1.0
    assert classifier["metrics"]["unsafe_escalations"] == 0
    assert classifier["metrics"]["grok_authorization_attempts"] == 0
    assert classifier["metrics"]["extra_call_frequency"] == 1.0
    assert classifier["metrics"]["fail_open_count"] == 5
    assert classifier["metrics"]["latency_ms"]["measurement"] == "local_adapter_simulation"
    assert classifier["metrics"]["remote_latency_ms"] == "not_tested"
    assert all(row["call_count"] == 1 for row in classifier["rows"])
    assert all(row["prompt_isolated"] for row in classifier["rows"])
    assert all(row["should_apply"] is False for row in classifier["rows"])

    session = report["session_simulation"]
    assert session == {
        "affinity_window": 2,
        "automatic_failures_before_fail_off": 3,
        "cache_domain_switches": "not_tested",
        "fail_off_activated": True,
        "route_flapping": 0,
        "route_sequence": ["deep", "deep", "deep"],
    }


def test_eval_cli_writes_reproducible_report(tmp_path):
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_turn_router.py"),
            "--corpus",
            str(CORPUS),
            "--output",
            str(output),
            "--classifier-corpus",
            str(CLASSIFIER_CORPUS),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["metrics"]["accuracy"] == 1.0
    assert report["metrics"]["unsafe_escalations"] == 0
    assert report["classifier_simulation"]["metrics"]["accuracy"] == 1.0
    assert "user_text" not in output.read_text(encoding="utf-8")
    assert str(output) in completed.stdout
