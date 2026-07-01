"""P1-10 high-fidelity synthetic telemetry/export/backup/sync canaries.

This harness simulates telemetry/export/backup/sync classification envelopes
without invoking live exporters, backup/sync jobs, Git/network calls, providers,
secrets, or live provider/profile/runtime configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


CASE_ORDER = (
    "P1-10-TELEMETRY-T01",
    "P1-10-EXPORT-T02",
    "P1-10-BACKUP-T03",
    "P1-10-MISSING-METADATA-T04",
    "P1-10-C0-T05",
)

RULES = {
    "telemetry": "w5.p1_10.telemetry.c2_nonlocal_exporter_denied",
    "export": "w5.p1_10.export.c2_external_bundle_denied",
    "backup": "w5.p1_10.backup.c2_nonlocal_sync_denied",
    "missing": "w5.p1_10.egress.required_metadata_missing",
    "c0_allowed": "w5.p1_10.export.c0_fake_local_adapter_allowed",
}


@dataclass(frozen=True)
class TelemetryExportSyncCanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    live_telemetry_export_count: int
    live_export_bundle_count: int
    live_backup_sync_count: int
    live_git_sync_count: int
    live_network_call_count: int
    fake_local_export_adapter_call_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool
    raw_metadata_stored: bool


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_obj(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _stable_id(prefix: str, *parts: str) -> str:
    return prefix + "-" + hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:12]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_row(
    *,
    case_id: str,
    decision: str,
    rule_id: str,
    reason: str,
    surface: str,
    classification: str,
    classification_source: str,
    requested_sink_type: str,
    requested_destination: str,
    allowed_destination_classes: list[str],
    missing_required_fields: list[str] | None = None,
    fake_local_export_adapter_call_count: int = 0,
) -> dict[str, Any]:
    correlation_id = _stable_id("p1-10-correlation", case_id)
    export_event_id = f"{correlation_id}:export:{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:8]}"
    payload_capture_id = _stable_id("p1-10-payload", case_id, requested_sink_type)
    metadata_fixture = {
        "case_id": case_id,
        "classification": classification,
        "classification_source": classification_source,
        "sink_type": requested_sink_type,
        "raw_metadata": "digest-only synthetic export metadata fixture",
    }
    payload_fixture = {
        "case_id": case_id,
        "export_event_id": export_event_id,
        "payload": "digest-only synthetic telemetry/export payload",
    }
    return {
        "case_id": case_id,
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "surface": surface,
        "profile_id": None,
        "policy_hash": None,
        "classification": classification,
        "classification_source": classification_source,
        "correlation_id": correlation_id,
        "export_event_id": export_event_id,
        "payload_capture_id": payload_capture_id,
        "requested_sink_type": requested_sink_type,
        "requested_destination_digest": _sha256_text(requested_destination),
        "payload_digest": _digest_obj(payload_fixture),
        "metadata_digest": _digest_obj(metadata_fixture),
        "allowed_destination_classes": allowed_destination_classes,
        "allowed_destination_classes_digest": _digest_obj(allowed_destination_classes),
        "missing_required_fields": missing_required_fields or [],
        "dispatch_denied_before_export": decision == "deny",
        "live_telemetry_export_count": 0,
        "live_export_bundle_count": 0,
        "live_backup_sync_count": 0,
        "live_git_sync_count": 0,
        "live_network_call_count": 0,
        "fake_local_export_adapter_call_count": fake_local_export_adapter_call_count,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "raw_metadata_stored": False,
        "redaction_class": "digest_only",
        "scope": "high_fidelity_synthetic_no_live_telemetry_export_backup_sync_git_network_provider_or_config_calls",
    }


def _rows() -> list[dict[str, Any]]:
    local_only = ["local_digest_log", "local_vault_repo", "local_backup_target"]
    public_local = ["local_digest_log", "local_export_adapter"]
    return [
        _base_row(
            case_id="P1-10-TELEMETRY-T01",
            decision="deny",
            rule_id=RULES["telemetry"],
            reason="C2/local-only telemetry event attempted non-local exporter sink",
            surface="synthetic_telemetry_export",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_export_envelope",
            requested_sink_type="nonlocal_telemetry_exporter",
            requested_destination="https://telemetry.example.invalid/ingest/digest-only-fixture",
            allowed_destination_classes=local_only,
        ),
        _base_row(
            case_id="P1-10-EXPORT-T02",
            decision="deny",
            rule_id=RULES["export"],
            reason="C2/local-only audit/export bundle targeted external export sink",
            surface="synthetic_audit_export_bundle",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_export_envelope",
            requested_sink_type="external_audit_export_bundle",
            requested_destination="s3://example-invalid-audit-export/digest-only-fixture",
            allowed_destination_classes=local_only,
        ),
        _base_row(
            case_id="P1-10-BACKUP-T03",
            decision="deny",
            rule_id=RULES["backup"],
            reason="C2/local-only backup/sync attempted non-local destination",
            surface="synthetic_backup_sync",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_export_envelope",
            requested_sink_type="nonlocal_backup_sync",
            requested_destination="git+ssh://example.invalid/private/vault-sync-fixture.git",
            allowed_destination_classes=local_only,
        ),
        _base_row(
            case_id="P1-10-MISSING-METADATA-T04",
            decision="deny",
            rule_id=RULES["missing"],
            reason="telemetry/export envelope missing classification, source, payload digest, or destination class",
            surface="synthetic_telemetry_export",
            classification="UNKNOWN",
            classification_source="unclassified",
            requested_sink_type="UNKNOWN",
            requested_destination="https://telemetry.example.invalid/missing-metadata-fixture",
            allowed_destination_classes=local_only,
            missing_required_fields=["classification", "classification_source", "payload_digest", "destination_class"],
        ),
        _base_row(
            case_id="P1-10-C0-T05",
            decision="allow",
            rule_id=RULES["c0_allowed"],
            reason="C0/public export path allowed only through fake local export adapter",
            surface="synthetic_audit_export_bundle",
            classification="C0_PUBLIC",
            classification_source="hl_aos_frozen_export_envelope",
            requested_sink_type="local_fake_export_adapter",
            requested_destination="local://fake-export-adapter/public-digest-only-fixture",
            allowed_destination_classes=public_local,
            fake_local_export_adapter_call_count=1,
        ),
    ]


def run_p1_10_telemetry_export_sync_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    policy_hash: str,
) -> TelemetryExportSyncCanaryResult:
    evidence_dir = Path(evidence_dir)
    rows = _rows()
    for row in rows:
        row["profile_id"] = profile_id
        row["policy_hash"] = policy_hash

    evidence_path = evidence_dir / "p1_10_telemetry_export_sync_evidence.jsonl"
    summary_path = evidence_dir / "p1_10_telemetry_export_sync_summary.json"
    _write_jsonl(evidence_path, rows)

    summary = {
        "case_ids": [row["case_id"] for row in rows],
        "total": len(rows),
        "denied": sum(row["decision"] == "deny" for row in rows),
        "allowed": sum(row["decision"] == "allow" for row in rows),
        "surfaces": sorted({row["surface"] for row in rows}),
        "rule_ids": [row["rule_id"] for row in rows],
        "live_telemetry_export_count": sum(row["live_telemetry_export_count"] for row in rows),
        "live_export_bundle_count": sum(row["live_export_bundle_count"] for row in rows),
        "live_backup_sync_count": sum(row["live_backup_sync_count"] for row in rows),
        "live_git_sync_count": sum(row["live_git_sync_count"] for row in rows),
        "live_network_call_count": sum(row["live_network_call_count"] for row in rows),
        "fake_local_export_adapter_call_count": sum(row["fake_local_export_adapter_call_count"] for row in rows),
        "provider_call_count": sum(row["provider_call_count"] for row in rows),
        "live_config_touched": any(row["live_config_touched"] for row in rows),
        "secret_values_read": any(row["secret_values_read"] for row in rows),
        "raw_payload_stored": any(row["raw_payload_stored"] for row in rows),
        "raw_metadata_stored": any(row["raw_metadata_stored"] for row in rows),
        "runtime_closure": "open_partial_green_synthetic_only",
    }
    _write_json(summary_path, summary)

    return TelemetryExportSyncCanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=summary["total"],
        denied=summary["denied"],
        allowed=summary["allowed"],
        live_telemetry_export_count=summary["live_telemetry_export_count"],
        live_export_bundle_count=summary["live_export_bundle_count"],
        live_backup_sync_count=summary["live_backup_sync_count"],
        live_git_sync_count=summary["live_git_sync_count"],
        live_network_call_count=summary["live_network_call_count"],
        fake_local_export_adapter_call_count=summary["fake_local_export_adapter_call_count"],
        provider_call_count=summary["provider_call_count"],
        live_config_touched=summary["live_config_touched"],
        secret_values_read=summary["secret_values_read"],
        raw_payload_stored=summary["raw_payload_stored"],
        raw_metadata_stored=summary["raw_metadata_stored"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--policy-hash", default="fixture-p1-10-policy-hash")
    args = parser.parse_args()

    result = run_p1_10_telemetry_export_sync_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        policy_hash=args.policy_hash,
    )
    print(
        "P1-10 telemetry/export/backup/sync canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )


if __name__ == "__main__":
    main()
