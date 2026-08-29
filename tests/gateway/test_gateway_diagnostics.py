from pathlib import Path

from gateway.shutdown_forensics import snapshot_shutdown_context
from scripts.hermes_gateway_diagnostics import collect


def test_shutdown_snapshot_contains_resource_and_task_evidence():
    snapshot = snapshot_shutdown_context(
        shutdown_reason="test",
        active_task_ids=["a", "b"],
        queued_task_ids=["c"],
        worker_pids=[123],
    )

    assert snapshot["shutdown_reason"] == "test"
    assert snapshot["active_agent_count"] == 2
    assert snapshot["queued_task_count"] == 1
    assert snapshot["worker_pids"] == [123]
    assert "gateway_rss" in snapshot
    assert "host_mem_available_kb" in snapshot
    assert "cgroup" in snapshot


def test_diagnostic_script_is_bounded_and_secret_safe():
    text = Path("scripts/hermes_gateway_diagnostics.py").read_text(encoding="utf-8")
    assert "environ" not in text
    assert "workers[:100]" not in text  # rows are sliced, not an unbounded command output
    assert "worker_rows[:100]" in text


def test_diagnostic_reads_bounded_admission_runtime_status(tmp_path, monkeypatch):
    (tmp_path / "gateway_state.json").write_text(
        '{"gateway_state":"running","active_agents":2,"admission":'
        '{"active_workers":2,"queued_tasks":1,"queued_task_ids":["task-3"]},'
        '"updated_at":"now","secret":"must-not-copy"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.hermes_gateway_diagnostics._service_pid", lambda _unit: None
    )

    result = collect(hermes_home=tmp_path)

    assert result["runtime_status"]["active_agents"] == 2
    assert result["runtime_status"]["admission"]["queued_tasks"] == 1
    assert "secret" not in result["runtime_status"]
