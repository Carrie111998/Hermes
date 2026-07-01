"""A1.8 execute_code nested write canary evidence validator."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.a1_8_execute_code_write_canaries import run_a1_8_execute_code_write_canaries


def test_a1_8_execute_code_write_canary_evidence() -> None:
    result = run_a1_8_execute_code_write_canaries()

    assert result.total == 5
    assert result.denied == 3
    assert result.allowed == 2
    assert result.sandbox_spawn_count == 0
    assert result.nested_tool_dispatch_count == 0
    assert result.target_write_count == 0
    assert result.provider_call_count == 0
    assert not result.live_config_touched
    assert not result.secret_values_read
    assert not result.raw_code_stored

    evidence_path = Path(result.evidence_path)
    assert evidence_path.exists()
    raw = evidence_path.read_text(encoding="utf-8")
    assert raw.strip()

    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(rows) == 5

    required = {
        "case_id", "decision", "reason", "classification",
        "classification_source", "intent", "code_digest",
        "sandbox_spawn_count", "nested_tool_dispatch_count", "target_write_count",
        "provider_call_count", "live_config_touched", "secret_values_read",
        "raw_code_stored",
    }
    for row in rows:
        assert required <= set(row)
        digest = row["code_digest"]
        assert isinstance(digest, str)
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64
        int(digest.removeprefix("sha256:"), 16)
        assert row["sandbox_spawn_count"] == 0
        assert row["nested_tool_dispatch_count"] == 0
        assert row["target_write_count"] == 0
        assert row["provider_call_count"] == 0
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_code_stored"] is False

    assert "Path('/tmp/a18-raw')" not in raw
    assert "write_file('/tmp/a18-rpc'" not in raw
    assert "public analysis" not in raw
    assert "internal analysis" not in raw

    by_id = {row["case_id"]: row for row in rows}
    assert set(by_id) == {"A1.8-T01", "A1.8-T02", "A1.8-T03", "A1.8-T04", "A1.8-T05"}
    assert by_id["A1.8-T01"]["decision"] == "denied"
    assert by_id["A1.8-T01"]["intent"] == "raw_python_file_write"
    assert by_id["A1.8-T02"]["decision"] == "denied"
    assert by_id["A1.8-T02"]["intent"] == "sandbox_rpc_write_file"
    assert by_id["A1.8-T03"]["decision"] == "denied"
    assert "no HL-AOS classification" in by_id["A1.8-T03"]["reason"]
    assert by_id["A1.8-T04"]["decision"] == "allowed"
    assert by_id["A1.8-T04"]["classification"] == "C0"
    assert by_id["A1.8-T05"]["decision"] == "allowed"
    assert by_id["A1.8-T05"]["classification"] == "C1"

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["total_cases"] == 5
    assert summary["denied"] == 3
    assert summary["allowed"] == 2
    assert summary["sandbox_spawn_count"] == 0
    assert summary["nested_tool_dispatch_count"] == 0
    assert summary["target_write_count"] == 0
