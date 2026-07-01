"""W10 RAG route-taint proof: retrieved chunks recompute taint before dispatch.

Fixture-only canaries: no live Qdrant, model provider, gateway, proxy, provider
configuration, or secret source is touched. Synthetic retrieved chunks are enough
to prove that post-retrieval taint is monotonic and deny-before-dispatch for
frontier/write sinks.
"""

import json
from pathlib import Path

from hermes_cli.w10_rag_route_taint import run_w10_rag_route_taint_canaries


EXPECTED_RULES = {
    "W10-T01": "w10.rag_taint.c2_chunk_frontier_denied",
    "W10-T02": "w10.rag_taint.incomplete_metadata_fail_closed",
    "W10-T03": "w10.rag_taint.stale_chunk_quarantined",
    "W10-T04": "w10.rag_taint.retrieved_instruction_cannot_authorize_sink",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_w10_t01_t04_retrieved_chunks_raise_taint_and_deny_before_dispatch(tmp_path):
    result = run_w10_rag_route_taint_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        query_classification="C0",
        policy_manifest_hash="fixture-w10-policy-hash",
    )

    assert result.total == 4
    assert result.denied == 4
    assert result.frontier_call_count == 0
    assert result.prompt_dispatch_count == 0
    assert result.live_qdrant_touched is False
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False
    assert Path(result.evidence_path).exists()
    assert Path(result.summary_path).exists()

    rows = _rows(Path(result.evidence_path))
    assert [row["case_id"] for row in rows] == ["W10-T01", "W10-T02", "W10-T03", "W10-T04"]

    for row in rows:
        case_id = row["case_id"]
        assert row["decision"] == "deny"
        assert row["rule_id"] == EXPECTED_RULES[case_id]
        assert row["query_classification"] == "C0"
        assert row["max_classification"] == "C2"
        assert row["session_taint"] == "C2"
        assert row["frontier_call_count"] == 0
        assert row["prompt_dispatch_count"] == 0
        assert row["live_qdrant_touched"] is False
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_payload_stored"] is False
        assert row["policy_manifest_hash"] == "fixture-w10-policy-hash"

    by_case = {row["case_id"]: row for row in rows}

    assert by_case["W10-T01"]["retrieved_chunk_count"] == 2
    assert by_case["W10-T01"]["quarantined_chunk_count"] == 0
    assert by_case["W10-T01"]["chunk_classifications"] == ["C0", "C2"]
    assert by_case["W10-T01"]["frontier_denied_before_prompt"] is True

    assert by_case["W10-T02"]["retrieved_chunk_count"] == 1
    assert by_case["W10-T02"]["quarantined_chunk_count"] == 1
    assert "INCOMPLETE_RAG_METADATA" in by_case["W10-T02"]["taint_flags"]
    assert by_case["W10-T02"]["metadata_valid"] is False

    assert by_case["W10-T03"]["retrieved_chunk_count"] == 1
    assert by_case["W10-T03"]["quarantined_chunk_count"] == 1
    assert by_case["W10-T03"]["freshness_status"] == "stale_ttl_or_source"
    assert "STALE_RAG_CHUNK" in by_case["W10-T03"]["taint_flags"]

    assert by_case["W10-T04"]["retrieved_chunk_count"] == 1
    assert by_case["W10-T04"]["quarantined_chunk_count"] == 0
    assert by_case["W10-T04"]["retrieved_instruction_present"] is True
    assert by_case["W10-T04"]["write_sink_authorized_by_retrieval"] is False
    assert by_case["W10-T04"]["write_sink_denied"] is True

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["case_ids"] == ["W10-T01", "W10-T02", "W10-T03", "W10-T04"]
    assert summary["total"] == 4
    assert summary["denied"] == 4
    assert summary["frontier_call_count"] == 0
    assert summary["prompt_dispatch_count"] == 0
    assert summary["live_qdrant_touched"] is False
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
