"""P1-10 telemetry/export/backup/sync classification canary proof.

High-fidelity synthetic only: no live telemetry, export, backup, sync,
network, provider dispatch, profile/provider config mutation, or secret inspection.
"""

import json
from pathlib import Path

from hermes_cli.p1_10_telemetry_export_sync_canaries import run_p1_10_telemetry_export_sync_canaries


EXPECTED_RULES = {
    "P1-10-TELEMETRY-T01": "w5.p1_10.telemetry.c2_nonlocal_exporter_denied",
    "P1-10-EXPORT-T02": "w5.p1_10.export.c2_external_bundle_denied",
    "P1-10-BACKUP-T03": "w5.p1_10.backup.c2_nonlocal_sync_denied",
    "P1-10-MISSING-METADATA-T04": "w5.p1_10.egress.required_metadata_missing",
    "P1-10-C0-T05": "w5.p1_10.export.c0_fake_local_adapter_allowed",
}

REQUIRED_FIELDS = {
    "case_id",
    "decision",
    "rule_id",
    "surface",
    "classification",
    "classification_source",
    "correlation_id",
    "export_event_id",
    "payload_capture_id",
    "requested_sink_type",
    "requested_destination_digest",
    "payload_digest",
    "metadata_digest",
    "allowed_destination_classes_digest",
    "dispatch_denied_before_export",
    "live_telemetry_export_count",
    "live_export_bundle_count",
    "live_backup_sync_count",
    "live_git_sync_count",
    "live_network_call_count",
    "fake_local_export_adapter_call_count",
    "provider_call_count",
    "live_config_touched",
    "secret_values_read",
    "raw_payload_stored",
    "raw_metadata_stored",
    "redaction_class",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p1_10_telemetry_export_backup_sync_envelopes_deny_c2_nonlocal_sinks(tmp_path):
    result = run_p1_10_telemetry_export_sync_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        policy_hash="fixture-p1-10-policy-hash",
    )

    assert result.total == 5
    assert result.denied == 4
    assert result.allowed == 1
    assert result.live_telemetry_export_count == 0
    assert result.live_export_bundle_count == 0
    assert result.live_backup_sync_count == 0
    assert result.live_git_sync_count == 0
    assert result.live_network_call_count == 0
    assert result.fake_local_export_adapter_call_count == 1
    assert result.provider_call_count == 0
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False
    assert result.raw_metadata_stored is False

    evidence_path = Path(result.evidence_path)
    summary_path = Path(result.summary_path)
    assert evidence_path.exists()
    assert summary_path.exists()

    rows = _rows(evidence_path)
    assert [row["case_id"] for row in rows] == [
        "P1-10-TELEMETRY-T01",
        "P1-10-EXPORT-T02",
        "P1-10-BACKUP-T03",
        "P1-10-MISSING-METADATA-T04",
        "P1-10-C0-T05",
    ]

    for row in rows:
        assert REQUIRED_FIELDS.issubset(row.keys())
        assert row["rule_id"] == EXPECTED_RULES[row["case_id"]]
        assert row["profile_id"] == "architect-fixture"
        assert row["policy_hash"] == "fixture-p1-10-policy-hash"
        assert row["correlation_id"].startswith("p1-10-correlation-")
        assert row["export_event_id"].startswith(row["correlation_id"] + ":export:")
        assert row["payload_capture_id"].startswith("p1-10-payload-")
        assert row["requested_destination_digest"].startswith("sha256:")
        assert row["payload_digest"].startswith("sha256:")
        assert row["metadata_digest"].startswith("sha256:")
        assert row["allowed_destination_classes_digest"].startswith("sha256:")
        assert row["live_telemetry_export_count"] == 0
        assert row["live_export_bundle_count"] == 0
        assert row["live_backup_sync_count"] == 0
        assert row["live_git_sync_count"] == 0
        assert row["live_network_call_count"] == 0
        assert row["provider_call_count"] == 0
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_payload_stored"] is False
        assert row["raw_metadata_stored"] is False
        assert row["redaction_class"] == "digest_only"

    by_case = {row["case_id"]: row for row in rows}

    telemetry = by_case["P1-10-TELEMETRY-T01"]
    assert telemetry["decision"] == "deny"
    assert telemetry["surface"] == "synthetic_telemetry_export"
    assert telemetry["classification"] == "C2_LOCAL_ONLY"
    assert telemetry["requested_sink_type"] == "nonlocal_telemetry_exporter"
    assert telemetry["dispatch_denied_before_export"] is True

    export = by_case["P1-10-EXPORT-T02"]
    assert export["decision"] == "deny"
    assert export["surface"] == "synthetic_audit_export_bundle"
    assert export["classification"] == "C2_LOCAL_ONLY"
    assert export["requested_sink_type"] == "external_audit_export_bundle"
    assert export["dispatch_denied_before_export"] is True

    backup = by_case["P1-10-BACKUP-T03"]
    assert backup["decision"] == "deny"
    assert backup["surface"] == "synthetic_backup_sync"
    assert backup["classification"] == "C2_LOCAL_ONLY"
    assert backup["requested_sink_type"] == "nonlocal_backup_sync"
    assert backup["dispatch_denied_before_export"] is True

    missing = by_case["P1-10-MISSING-METADATA-T04"]
    assert missing["decision"] == "deny"
    assert missing["classification"] == "UNKNOWN"
    assert missing["classification_source"] == "unclassified"
    assert missing["missing_required_fields"] == ["classification", "classification_source", "payload_digest", "destination_class"]
    assert missing["dispatch_denied_before_export"] is True

    positive = by_case["P1-10-C0-T05"]
    assert positive["decision"] == "allow"
    assert positive["classification"] == "C0_PUBLIC"
    assert positive["requested_sink_type"] == "local_fake_export_adapter"
    assert positive["dispatch_denied_before_export"] is False
    assert positive["fake_local_export_adapter_call_count"] == 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_ids"] == [
        "P1-10-TELEMETRY-T01",
        "P1-10-EXPORT-T02",
        "P1-10-BACKUP-T03",
        "P1-10-MISSING-METADATA-T04",
        "P1-10-C0-T05",
    ]
    assert summary["total"] == 5
    assert summary["denied"] == 4
    assert summary["allowed"] == 1
    assert summary["live_telemetry_export_count"] == 0
    assert summary["live_export_bundle_count"] == 0
    assert summary["live_backup_sync_count"] == 0
    assert summary["live_git_sync_count"] == 0
    assert summary["live_network_call_count"] == 0
    assert summary["fake_local_export_adapter_call_count"] == 1
    assert summary["provider_call_count"] == 0
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
    assert summary["raw_metadata_stored"] is False
    assert summary["runtime_closure"] == "open_partial_green_synthetic_only"

    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "confidential telemetry raw payload" not in evidence_text
    assert "SECRET" not in evidence_text
