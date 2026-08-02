"""Regression tests for the real-process team-memory A/B runner."""

from __future__ import annotations

from pathlib import Path

import yaml

from experiments.team_memory_ab_test.scripts.run_experiment import (
    TaskResult,
    _configure_arm,
    _scrub_sensitive_files,
    analyze,
    create_test_tasks,
)


def _result(
    task_id: str,
    arm: str,
    *,
    seconds: float,
    success: bool,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        agent_type=arm,
        returncode=0 if success else 1,
        time_seconds=seconds,
        api_calls=1,
        total_tokens=100,
        memory_tool_calls=1 if arm == "enhanced" else 0,
        success=success,
        keywords_found=["expected"] if success else [],
        keywords_missing=[] if success else ["expected"],
        response_preview="response",
        response_sha256="hash",
    )


def test_task_catalog_and_repeated_sample_analysis_are_explicit():
    tasks = create_test_tasks()
    assert len(tasks) == 20
    assert {task.category for task in tasks} == {"backend", "frontend", "devops"}

    from experiments.team_memory_ab_test.scripts.run_experiment import ComparisonResult

    results = [
        ComparisonResult(
            task_id=f"task#r{index}",
            repetition=index,
            arm_order=["baseline", "enhanced"],
            baseline=_result(f"task#r{index}", "baseline", seconds=10.0, success=True),
            enhanced=_result(f"task#r{index}", "enhanced", seconds=8.0, success=True),
            time_delta_seconds=2.0,
            api_calls_delta=0,
            token_delta=0,
            success_delta=0,
        )
        for index in range(1, 3)
    ]
    assert analyze(results, minimum_pairs=3)["decision"] == "insufficient_sample"
    report = analyze(results, minimum_pairs=2)
    assert report["decision"] == "candidate_go"
    assert report["paired_success_ties"] == 2
    assert report["median_time_saving_ratio"] == 0.2


def test_arm_configuration_isolated_and_credentials_scrubbed(tmp_path):
    source_home = tmp_path / "source"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["other-plugin"]}}),
        encoding="utf-8",
    )
    (source_home / "auth.json").write_text("{\"token\": \"secret\"}", encoding="utf-8")

    baseline_home = tmp_path / "baseline"
    _configure_arm(
        source_home,
        baseline_home,
        arm="baseline",
        db_path=baseline_home / "memory.db",
        metrics_path=baseline_home / "metrics.db",
        workspace_id="xinxiang",
    )
    baseline_config = yaml.safe_load(
        (baseline_home / "config.yaml").read_text(encoding="utf-8")
    )
    assert baseline_config["team_memory"]["enabled"] is False
    assert "team-memory" not in baseline_config["plugins"]["enabled"]

    enhanced_home = tmp_path / "enhanced"
    _configure_arm(
        source_home,
        enhanced_home,
        arm="enhanced",
        db_path=enhanced_home / "memory.db",
        metrics_path=enhanced_home / "metrics.db",
        workspace_id="xinxiang",
    )
    enhanced_config = yaml.safe_load(
        (enhanced_home / "config.yaml").read_text(encoding="utf-8")
    )
    assert enhanced_config["team_memory"]["enabled"] is True
    assert "team-memory" in enhanced_config["plugins"]["enabled"]

    _scrub_sensitive_files(enhanced_home)
    assert not (enhanced_home / "auth.json").exists()
