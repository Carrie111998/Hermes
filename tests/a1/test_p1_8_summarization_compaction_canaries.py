"""P1-8 summarization/compaction taint and dispatch-gate canary proof.

High-fidelity synthetic only: no live compaction, summarizer/provider dispatch,
profile/provider config mutation, or secret inspection.
"""

import json
from pathlib import Path

from hermes_cli.p1_8_summarization_compaction_canaries import run_p1_8_summarization_compaction_canaries


EXPECTED_RULES = {
    "P1-8-C2-SUMMARY-T01": "w5.p1_8.summarization.c2_frontier_summary_denied",
    "P1-8-MISSING-TAINT-T02": "w5.p1_8.summarization.required_taint_missing",
    "P1-8-DOWNGRADE-T03": "w5.p1_8.summarization.summary_taint_downgrade_denied",
    "P1-8-AUX-T04": "w5.p1_8.compaction.nonlocal_aux_model_denied",
    "P1-8-C0-T05": "w5.p1_8.summarization.c0_fake_local_summarizer_allowed",
}

REQUIRED_FIELDS = {
    "case_id",
    "decision",
    "rule_id",
    "surface",
    "session_classification",
    "summary_classification",
    "classification_source",
    "summary_taint_inherited",
    "monotonic_taint_ok",
    "session_correlation_id",
    "summary_correlation_id",
    "compaction_event_id",
    "payload_capture_id",
    "source_transcript_digest",
    "summary_payload_digest",
    "summary_output_digest",
    "requested_summarizer_target",
    "resolved_summarizer_target",
    "requested_model_tier",
    "max_allowed_model_tier",
    "summarizer_dispatch_count",
    "fake_local_summarizer_adapter_call_count",
    "provider_call_count",
    "live_compaction_invoked",
    "live_config_touched",
    "secret_values_read",
    "raw_payload_stored",
    "raw_summary_stored",
    "redaction_class",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p1_8_summarization_compaction_denies_nonlocal_or_untainted_summary_paths(tmp_path):
    result = run_p1_8_summarization_compaction_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        policy_hash="fixture-p1-8-policy-hash",
    )

    assert result.total == 5
    assert result.denied == 4
    assert result.allowed == 1
    assert result.summarizer_dispatch_count == 0
    assert result.fake_local_summarizer_adapter_call_count == 1
    assert result.provider_call_count == 0
    assert result.live_compaction_invoked is False
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False
    assert result.raw_summary_stored is False

    evidence_path = Path(result.evidence_path)
    summary_path = Path(result.summary_path)
    assert evidence_path.exists()
    assert summary_path.exists()

    rows = _rows(evidence_path)
    assert [row["case_id"] for row in rows] == [
        "P1-8-C2-SUMMARY-T01",
        "P1-8-MISSING-TAINT-T02",
        "P1-8-DOWNGRADE-T03",
        "P1-8-AUX-T04",
        "P1-8-C0-T05",
    ]

    for row in rows:
        assert REQUIRED_FIELDS.issubset(row.keys())
        assert row["rule_id"] == EXPECTED_RULES[row["case_id"]]
        assert row["profile_id"] == "architect-fixture"
        assert row["policy_hash"] == "fixture-p1-8-policy-hash"
        assert row["surface"] in {"synthetic_summarization", "synthetic_compaction"}
        assert row["session_correlation_id"].startswith("p1-8-session-")
        assert row["summary_correlation_id"].startswith(row["session_correlation_id"] + ":summary:")
        assert row["compaction_event_id"].startswith("p1-8-compact-")
        assert row["payload_capture_id"].startswith("p1-8-payload-")
        assert row["source_transcript_digest"].startswith("sha256:")
        assert row["summary_payload_digest"].startswith("sha256:")
        assert row["summary_output_digest"].startswith("sha256:")
        assert row["provider_call_count"] == 0
        assert row["live_compaction_invoked"] is False
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_payload_stored"] is False
        assert row["raw_summary_stored"] is False
        assert row["redaction_class"] == "digest_only"

    by_case = {row["case_id"]: row for row in rows}

    c2_frontier = by_case["P1-8-C2-SUMMARY-T01"]
    assert c2_frontier["decision"] == "deny"
    assert c2_frontier["session_classification"] == "C2_LOCAL_ONLY"
    assert c2_frontier["summary_classification"] == "C2_LOCAL_ONLY"
    assert c2_frontier["classification_source"] == "hl_aos_frozen_session_taint"
    assert c2_frontier["summary_taint_inherited"] is True
    assert c2_frontier["monotonic_taint_ok"] is True
    assert c2_frontier["requested_summarizer_target"] == "frontier"
    assert c2_frontier["resolved_summarizer_target"] is None
    assert c2_frontier["requested_model_tier"] == "L2_FRONTIER"
    assert c2_frontier["max_allowed_model_tier"] == "L0_LOCAL"
    assert c2_frontier["dispatch_denied_before_summarizer"] is True

    missing = by_case["P1-8-MISSING-TAINT-T02"]
    assert missing["decision"] == "deny"
    assert missing["session_classification"] == "UNKNOWN"
    assert missing["summary_classification"] == "UNKNOWN"
    assert missing["classification_source"] == "unclassified"
    assert missing["summary_taint_inherited"] is False
    assert missing["monotonic_taint_ok"] is False
    assert missing["missing_required_fields"] == ["session_classification", "classification_source"]
    assert missing["dispatch_denied_before_summarizer"] is True

    downgrade = by_case["P1-8-DOWNGRADE-T03"]
    assert downgrade["decision"] == "deny"
    assert downgrade["session_classification"] == "C2_LOCAL_ONLY"
    assert downgrade["summary_classification"] == "C0_PUBLIC"
    assert downgrade["summary_taint_inherited"] is False
    assert downgrade["monotonic_taint_ok"] is False
    assert downgrade["requested_summarizer_target"] == "local_fake_summarizer_adapter"
    assert downgrade["resolved_summarizer_target"] is None
    assert downgrade["dispatch_denied_before_summarizer"] is True

    aux = by_case["P1-8-AUX-T04"]
    assert aux["decision"] == "deny"
    assert aux["surface"] == "synthetic_compaction"
    assert aux["session_classification"] == "C2_LOCAL_ONLY"
    assert aux["requested_summarizer_target"] == "auxiliary_frontier_compression_model"
    assert aux["resolved_summarizer_target"] is None
    assert aux["requested_model_tier"] == "L2_FRONTIER"
    assert aux["max_allowed_model_tier"] == "L0_LOCAL"
    assert aux["dispatch_denied_before_summarizer"] is True

    positive = by_case["P1-8-C0-T05"]
    assert positive["decision"] == "allow"
    assert positive["session_classification"] == "C0_PUBLIC"
    assert positive["summary_classification"] == "C0_PUBLIC"
    assert positive["classification_source"] == "hl_aos_frozen_session_taint"
    assert positive["summary_taint_inherited"] is True
    assert positive["monotonic_taint_ok"] is True
    assert positive["requested_summarizer_target"] == "local_fake_summarizer_adapter"
    assert positive["resolved_summarizer_target"] == "local_fake_summarizer_adapter"
    assert positive["fake_local_summarizer_adapter_call_count"] == 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_ids"] == [
        "P1-8-C2-SUMMARY-T01",
        "P1-8-MISSING-TAINT-T02",
        "P1-8-DOWNGRADE-T03",
        "P1-8-AUX-T04",
        "P1-8-C0-T05",
    ]
    assert summary["total"] == 5
    assert summary["denied"] == 4
    assert summary["allowed"] == 1
    assert summary["summarizer_dispatch_count"] == 0
    assert summary["fake_local_summarizer_adapter_call_count"] == 1
    assert summary["provider_call_count"] == 0
    assert summary["live_compaction_invoked"] is False
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
    assert summary["raw_summary_stored"] is False
    assert summary["runtime_closure"] == "open_partial_green_synthetic_only"

    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "confidential transcript raw text" not in evidence_text
    assert "SECRET" not in evidence_text
