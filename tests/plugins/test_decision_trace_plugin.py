from __future__ import annotations

import importlib
import json
import sys


MODULE = "plugins.observability.decision_trace"


def fresh_module():
    sys.modules.pop(MODULE, None)
    return importlib.import_module(MODULE)


def test_manifest_and_plugin_are_present():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "plugins/observability/decision_trace"
    assert (root / "plugin.yaml").exists()
    assert (root / "__init__.py").exists()


def test_trace_is_local_metadata_only_and_aggregates_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    mod = fresh_module()

    mod.on_pre_api_request(
        task_id="task-1", session_id="session-1", turn_id="turn-1",
        api_request_id="api-1", model="local-model", provider="ollama",
    )
    mod.on_post_api_request(
        task_id="task-1", session_id="session-1", turn_id="turn-1",
        api_request_id="api-1", usage={
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_tokens": 10, "cache_write_tokens": 5,
        },
    )
    mod.on_post_tool_call(task_id="task-1", session_id="session-1", turn_id="turn-1")
    mod.on_post_llm_call(
        task_id="task-1", session_id="session-1", turn_id="turn-1",
        finish_reason="stop", assistant_tool_call_count=0,
        usage={"input_tokens": 999, "output_tokens": 999},
    )

    path = tmp_path / ".hermes/telemetry/decision-traces.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == "hermes.decision_trace.v1"
    assert record["trace_id"]
    assert record["model"] == "local-model"
    assert record["provider"] == "ollama"
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 20
    assert record["cache_read_tokens"] == 10
    assert record["cache_write_tokens"] == 5
    assert record["tool_calls"] == 1
    assert all(key not in record for key in ("prompt", "response", "args", "result"))


def test_trace_fail_open_when_path_cannot_be_written(monkeypatch):
    mod = fresh_module()
    monkeypatch.setattr(mod, "_path", lambda: __import__("pathlib").Path("/dev/null/trace.jsonl"))
    mod.on_pre_api_request(task_id="t", session_id="s", turn_id="x")
    mod.on_post_llm_call(task_id="t", session_id="s", turn_id="x", finish_reason="stop")
    # The observer must not make a task fail when local persistence is broken.
    assert True


def test_offline_evaluator_summarizes_and_compares(tmp_path):
    from plugins.observability.decision_trace.evaluate import compare, load_records, summarize

    left_path = tmp_path / "left.jsonl"
    right_path = tmp_path / "right.jsonl"
    left_path.write_text(json.dumps({
        "schema_version": "hermes.decision_trace.v1",
        "status": "completed",
        "duration_ms": 100,
        "input_tokens": 10,
        "output_tokens": 5,
    }) + "\n")
    right_path.write_text(json.dumps({
        "schema_version": "hermes.decision_trace.v1",
        "status": "completed",
        "duration_ms": 80,
        "input_tokens": 12,
        "output_tokens": 4,
    }) + "\n")

    assert summarize(load_records(left_path))["records"] == 1
    result = compare(load_records(left_path), load_records(right_path))
    assert result["right_minus_left"]["duration_ms"] == -20.0
    assert result["right_minus_left"]["input_tokens"] == 2.0
