from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.cognitive_memory_longitudinal_acceptance import (
    load_spec,
    run_acceptance,
    run_fresh_process_soak,
    write_report,
)


REQUIRED_METRICS = {
    "recall_at_8",
    "mrr_at_8",
    "ndcg_at_8",
    "negative_false_recall_rate",
    "negative_no_result_precision",
    "temporal_update_accuracy",
    "current_vs_history_accuracy",
    "superseded_leak_count",
    "cross_run_leak_count",
    "secret_recall_count",
    "memory_to_action_tool_selection_accuracy",
    "memory_to_action_parameter_grounding_accuracy",
    "correction_success",
    "selective_repair_success",
    "context_chars",
    "latency_ms",
    "state_mutation_count",
}


def test_all_longitudinal_scenarios_and_metrics_pass_on_temporary_databases(
    tmp_path: Path,
) -> None:
    spec = load_spec()
    report = run_acceptance(tmp_path / "acceptance", spec=spec)

    assert report["artifact_schema_version"] == spec["artifact_schema_version"]
    assert report["fixture_schema_version"] == spec["fixture_schema_version"]
    assert report["active_gate_state"] == "HOLD"
    assert set(report["metrics"]) == REQUIRED_METRICS
    assert report["scenario_results"] == {
        key: {"name": name, "passed": True}
        for key, name in spec["required_scenarios"].items()
    }

    metrics = report["metrics"]
    thresholds = spec["thresholds"]
    assert metrics["recall_at_8"] >= thresholds["recall_at_8"]
    assert metrics["mrr_at_8"] >= thresholds["mrr_at_8"]
    assert metrics["ndcg_at_8"] >= thresholds["ndcg_at_8"]
    assert (
        metrics["negative_false_recall_rate"]
        <= thresholds["negative_false_recall_rate"]
    )
    assert (
        metrics["negative_no_result_precision"]
        >= thresholds["negative_no_result_precision"]
    )
    assert metrics["temporal_update_accuracy"] == 1.0
    assert metrics["current_vs_history_accuracy"] == 1.0
    assert metrics["memory_to_action_tool_selection_accuracy"] == 1.0
    assert metrics["memory_to_action_parameter_grounding_accuracy"] == 1.0
    assert metrics["correction_success"] == 1.0
    assert metrics["selective_repair_success"] == 1.0
    assert metrics["superseded_leak_count"] == 0
    assert metrics["cross_run_leak_count"] == 0
    assert metrics["secret_recall_count"] == 0
    assert metrics["state_mutation_count"] == 0
    assert 0 < metrics["context_chars"] <= spec["max_context_chars"]
    assert set(metrics["latency_ms"]) == {"p50", "p95"}
    assert 0.0 <= metrics["latency_ms"]["p50"] <= metrics["latency_ms"]["p95"]
    assert report["database_integrity"] == {
        "ebbinghaus_before": "ok",
        "ebbinghaus_after": "ok",
        "semantic_graph_before": "ok",
        "semantic_graph_after": "ok",
    }

    output = tmp_path / "acceptance-report.json"
    write_report(report, output)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted == report
    serialized = json.dumps(persisted, ensure_ascii=False).lower()
    for forbidden in ("query\"", "content\"", "embedding\"", "vector\"", "secret\""):
        assert forbidden not in serialized


def test_repeated_fresh_process_soak_is_integral_and_idempotent(tmp_path: Path) -> None:
    spec = load_spec()
    report = run_fresh_process_soak(
        tmp_path / "soak",
        repetitions=spec["soak_repetitions"],
        python_executable=sys.executable,
    )

    assert report["fresh_process_restarts"] == spec["soak_repetitions"]
    assert report["process_exit_codes"] == [0] * spec["soak_repetitions"]
    assert report["integrity_checks"] == ["ok"] * (2 * spec["soak_repetitions"])
    assert report["dry_run_mutation_count"] == 0
    assert report["idempotent_apply_failures"] == 0
    assert report["unexpected_count_changes"] == 0
    assert report["process_or_handle_leak_count"] == 0
