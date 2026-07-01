"""CSR-006 T01-T04: local degradation must fail closed for C2+.

Fixture-only canaries: no live Ollama, LiteLLM, Headroom, gateway, proxy, or
provider calls are made. The harness simulates degradation signals and proves the
policy branch denies before frontier/outbound fallback for C2 workloads.
"""

import json
from pathlib import Path

from hermes_cli.csr006_degradation_guard import run_csr006_local_degradation_canaries


EXPECTED_RULES = {
    "CSR-006-T01": "csr006.local_ollama_unavailable.deny_c2",
    "CSR-006-T02": "csr006.compression_provider_failure.circuit_breaker",
    "CSR-006-T03": "csr006.c2_preflight_frontier_block",
    "CSR-006-T04": "csr006.frontier_failure_no_fallback",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_csr006_t01_t04_degradation_canaries_deny_c2_without_frontier_calls(tmp_path):
    result = run_csr006_local_degradation_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="concierge-fixture",
        classification="C2",
        policy_manifest_hash="fixture-policy-hash",
    )

    assert result.total == 4
    assert result.denied == 4
    assert result.frontier_call_count == 0
    assert result.retry_storm_detected is False
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert Path(result.evidence_path).exists()
    assert Path(result.summary_path).exists()

    rows = _rows(Path(result.evidence_path))
    assert [row["case_id"] for row in rows] == ["CSR-006-T01", "CSR-006-T02", "CSR-006-T03", "CSR-006-T04"]

    for row in rows:
        case_id = row["case_id"]
        assert row["decision"] == "deny"
        assert row["rule_id"] == EXPECTED_RULES[case_id]
        assert row["classification"] == "C2"
        assert row["frontier_call_count"] == 0
        assert row["alternate_frontier_retry_count"] == 0
        assert row["retry_storm_detected"] is False
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["policy_manifest_hash"] == "fixture-policy-hash"
        assert row["raw_payload_stored"] is False

    by_case = {row["case_id"]: row for row in rows}
    assert by_case["CSR-006-T01"]["local_provider_status"] == "unavailable"
    assert by_case["CSR-006-T01"]["gateway_responsive"] is True
    assert by_case["CSR-006-T01"]["explicit_error"] == "local_unavailable_c2_fail_closed"

    assert by_case["CSR-006-T02"]["compression_status"] == "provider_401"
    assert by_case["CSR-006-T02"]["compression_attempt_count"] == 1
    assert by_case["CSR-006-T02"]["polling_loop_continues"] is True
    assert by_case["CSR-006-T02"]["flood_control_triggered"] is False

    assert by_case["CSR-006-T03"]["preflight_blocked"] is True
    assert by_case["CSR-006-T03"]["model_dispatch_count"] == 0
    assert by_case["CSR-006-T03"]["audit_reason"] == "c2-pre-flight-block"

    assert by_case["CSR-006-T04"]["simulated_litellm_status"] == 503
    assert by_case["CSR-006-T04"]["fallback_attempted"] is False
    assert by_case["CSR-006-T04"]["explicit_error"] == "frontier_failure_no_c2_fallback"

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["total"] == 4
    assert summary["denied"] == 4
    assert summary["frontier_call_count"] == 0
    assert summary["retry_storm_detected"] is False
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
