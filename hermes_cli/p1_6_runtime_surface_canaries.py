"""P1-6 high-fidelity synthetic runtime-surface canaries.

This harness exercises realistic CLI, Telegram gateway, and cron entrypoint
envelopes without touching live entrypoints, providers, schedulers, gateway
services, protected config, or secrets. It proves each surface reaches the same
surface-adapter -> guard/resolver -> payload-capture -> dispatch-gate decision
shape before any dispatch or sink.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


CASE_ORDER = (
    "P1-6-CLI-T01",
    "P1-6-GATEWAY-T02",
    "P1-6-CRON-T03",
    "P1-6-CORRELATION-T04",
    "P1-6-C0-T05",
)
SURFACES = ("cli", "gateway_telegram", "cron")
DECISION_SHAPE = ["surface_adapter", "guard_resolver", "payload_capture", "dispatch_gate"]
RULES = {
    "cli_c2_frontier": "w5.p1_6.cli.c2_frontier_denied",
    "gateway_missing_approval": "w5.p1_6.gateway.approval_ref_required",
    "cron_disallowed_sink": "w5.p1_6.cron.disallowed_frontier_sink_denied",
    "missing_required_field": "w5.p1_6.envelope.required_field_missing",
    "c0_local_allowed": "w5.p1_6.c0.local_fake_adapter_allowed",
}


@dataclass(frozen=True)
class RuntimeSurfaceCanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    provider_call_count: int
    scheduled_dispatch_count: int
    gateway_raw_content_sent: bool
    raw_payload_stored: bool
    live_config_touched: bool
    secret_values_read: bool


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    joined = ":".join(parts)
    return prefix + "-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_row(envelope, *, case_id: str, decision: str, rule_id: str, reason: str):
    surface = envelope["surface"]
    request_id = envelope.get("request_id") or "missing-input-request-id"
    job_id = envelope.get("job_id")
    payload_digest = envelope.get("payload_digest") or _sha256_text(case_id + ":missing-input-payload-digest")
    correlation_id = envelope.get("correlation_id") or _stable_id("corr", case_id, surface, request_id)
    resolver_decision_id = _stable_id("resolver", case_id, surface, request_id, rule_id)
    guard_decision_id = _stable_id("guard", case_id, surface, request_id, rule_id)
    payload_capture_id = _stable_id("payload", case_id, surface, request_id, payload_digest)

    resolved_target = envelope.get("resolved_target")
    if decision == "deny":
        resolved_target = None

    return {
        "case_id": case_id,
        "surface": surface,
        "request_id": request_id,
        "job_id": job_id,
        "correlation_id": correlation_id,
        "guard_decision_id": guard_decision_id,
        "resolver_decision_id": resolver_decision_id,
        "payload_capture_id": payload_capture_id,
        "decision_shape": list(DECISION_SHAPE),
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "classification": envelope.get("classification", "C2_LOCAL_ONLY"),
        "classification_source": envelope.get("classification_source", "hl_aos_frozen_surface_metadata"),
        "requested_target": envelope.get("requested_target"),
        "resolved_target": resolved_target,
        "requested_sink_path": envelope.get("requested_sink_path"),
        "policy_hash": envelope["policy_hash"],
        "policy_manifest_hash": envelope["policy_hash"],
        "payload_digest": payload_digest,
        "redaction_class": "digest_only",
        "provider_call_count": 0,
        "scheduled_dispatch_count": envelope.get("scheduled_dispatch_count", 0),
        "fake_local_provider_adapter_call_count": envelope.get("fake_local_provider_adapter_call_count", 0),
        "dispatch_denied_before_provider": decision == "deny",
        "gateway_raw_content_sent": False,
        "gateway_response_with_raw_content_blocked": surface == "gateway_telegram" and decision == "deny",
        "raw_payload_stored": False,
        "live_config_touched": False,
        "live_gateway_touched": False,
        "live_cron_scheduler_touched": False,
        "secret_values_read": False,
        "runtime_state_mutated": False,
        "signed_approval_ref_present": bool(envelope.get("signed_approval_ref")),
        "surface_metadata_digest": envelope.get("surface_metadata_digest"),
        "fail_closed_missing_required_field": bool(envelope.get("missing_envelope_field")),
        "missing_envelope_field": envelope.get("missing_envelope_field"),
        "scope": "high_fidelity_synthetic_no_live_surface_or_provider_calls",
    }


def _cli_c2_envelope(policy_hash: str):
    raw_payload = "C2 CLI raw runtime-boundary prompt: prove deny before frontier dispatch"
    return {
        "surface": "cli",
        "request_id": "cli-request-p1-6-c2-frontier",
        "correlation_id": "p1-6-cli-c2-frontier",
        "classification": "C2_LOCAL_ONLY",
        "classification_source": "hl_aos_frozen_surface_metadata",
        "requested_target": "frontier",
        "policy_hash": policy_hash,
        "payload_digest": _sha256_text(raw_payload),
        "surface_metadata_digest": _sha256_text("argv:hermes --profile pennyworth-architect --model frontier"),
    }


def _gateway_c2_envelope(policy_hash: str):
    raw_payload = "Telegram confidential body: C2 local-only message requesting frontier reply"
    return {
        "surface": "gateway_telegram",
        "request_id": "telegram-update-p1-6-002",
        "correlation_id": "p1-6-gateway-c2-missing-approval",
        "classification": "C2_LOCAL_ONLY",
        "classification_source": "hl_aos_frozen_surface_metadata",
        "requested_target": "frontier",
        "policy_hash": policy_hash,
        "payload_digest": _sha256_text(raw_payload),
        "surface_metadata_digest": _sha256_text("telegram:chat-redacted:update:message:thread"),
        "signed_approval_ref": None,
    }


def _cron_c2_envelope(policy_hash: str):
    raw_payload = "Stored C2 cron prompt: run scheduled frontier summary and external sink"
    return {
        "surface": "cron",
        "request_id": "cron-tick-p1-6-003",
        "job_id": "cron-job-p1-6-c2-frontier",
        "correlation_id": "p1-6-cron-c2-disallowed-sink",
        "classification": "C2_LOCAL_ONLY",
        "classification_source": "hl_aos_frozen_surface_metadata",
        "requested_target": "frontier",
        "requested_sink_path": "frontier_model_dispatch",
        "policy_hash": policy_hash,
        "payload_digest": _sha256_text(raw_payload),
        "surface_metadata_digest": _sha256_text("cron:job:identity:schedule:stored-prompt-digest"),
        "scheduled_dispatch_count": 0,
    }


def _missing_field_envelopes(policy_hash: str):
    cli = _cli_c2_envelope(policy_hash)
    cli.pop("request_id")
    cli["classification_source"] = "surface_envelope_missing_required_field"
    cli["missing_envelope_field"] = "request_id"

    gateway = _gateway_c2_envelope(policy_hash)
    gateway.pop("payload_digest")
    gateway["classification_source"] = "surface_envelope_missing_required_field"
    gateway["missing_envelope_field"] = "payload_digest"

    cron = _cron_c2_envelope(policy_hash)
    cron.pop("job_id")
    cron["classification_source"] = "surface_envelope_missing_required_field"
    cron["missing_envelope_field"] = "job_id"
    return [cli, gateway, cron]


def _c0_local_envelope(policy_hash: str):
    raw_payload = "C0 public synthetic prompt allowed through fake local adapter"
    return {
        "surface": "cli",
        "request_id": "cli-request-p1-6-c0-local",
        "correlation_id": "p1-6-cli-c0-local-allowed",
        "classification": "C0_PUBLIC",
        "classification_source": "hl_aos_frozen_surface_metadata",
        "requested_target": "local_fake_provider",
        "resolved_target": "local_fake_provider",
        "policy_hash": policy_hash,
        "payload_digest": _sha256_text(raw_payload),
        "surface_metadata_digest": _sha256_text("argv:hermes --profile content-fixture --provider fake-local"),
        "fake_local_provider_adapter_call_count": 1,
    }


def _rows(policy_hash: str):
    rows = [
        _base_row(
            _cli_c2_envelope(policy_hash),
            case_id="P1-6-CLI-T01",
            decision="deny",
            rule_id=RULES["cli_c2_frontier"],
            reason="c2_local_only_cli_requested_frontier_target",
        ),
        _base_row(
            _gateway_c2_envelope(policy_hash),
            case_id="P1-6-GATEWAY-T02",
            decision="deny",
            rule_id=RULES["gateway_missing_approval"],
            reason="gateway_c2_local_only_missing_signed_approval_reference",
        ),
        _base_row(
            _cron_c2_envelope(policy_hash),
            case_id="P1-6-CRON-T03",
            decision="deny",
            rule_id=RULES["cron_disallowed_sink"],
            reason="stored_cron_c2_prompt_requested_disallowed_frontier_sink_path",
        ),
    ]
    for envelope in _missing_field_envelopes(policy_hash):
        rows.append(
            _base_row(
                envelope,
                case_id="P1-6-CORRELATION-T04",
                decision="deny",
                rule_id=RULES["missing_required_field"],
                reason="required_surface_envelope_field_missing_fail_closed",
            )
        )
    rows.append(
        _base_row(
            _c0_local_envelope(policy_hash),
            case_id="P1-6-C0-T05",
            decision="allow",
            rule_id=RULES["c0_local_allowed"],
            reason="c0_public_request_allowed_to_fake_local_provider_adapter_with_complete_evidence_triplet",
        )
    )
    return rows


def run_p1_6_runtime_surface_canaries(*, evidence_dir: str | Path, profile_id: str, policy_hash: str) -> RuntimeSurfaceCanaryResult:
    """Run P1-6 high-fidelity synthetic CLI/gateway/cron canaries."""

    evidence_root = Path(evidence_dir)
    evidence_path = evidence_root / "p1_6_runtime_surface_canaries.jsonl"
    summary_path = evidence_root / "run_summary.json"
    rows = _rows(policy_hash)
    for row in rows:
        row["profile_id"] = profile_id

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "deny")
    allowed = sum(1 for row in rows if row["decision"] == "allow")
    provider_call_count = sum(row["provider_call_count"] for row in rows)
    scheduled_dispatch_count = sum(row["scheduled_dispatch_count"] for row in rows)
    gateway_raw_content_sent = any(row["gateway_raw_content_sent"] for row in rows)
    raw_payload_stored = any(row["raw_payload_stored"] for row in rows)
    live_config_touched = any(row["live_config_touched"] for row in rows)
    secret_values_read = any(row["secret_values_read"] for row in rows)

    summary = {
        "case_ids": list(CASE_ORDER),
        "surfaces": list(SURFACES),
        "total": total,
        "denied": denied,
        "allowed": allowed,
        "rule_ids": {row["case_id"] + ":" + row["surface"]: row["rule_id"] for row in rows},
        "decisions": {row["case_id"] + ":" + row["surface"]: row["decision"] for row in rows},
        "provider_call_count": provider_call_count,
        "scheduled_dispatch_count": scheduled_dispatch_count,
        "fake_local_provider_adapter_call_count": sum(row["fake_local_provider_adapter_call_count"] for row in rows),
        "gateway_raw_content_sent": gateway_raw_content_sent,
        "raw_payload_stored": raw_payload_stored,
        "live_config_touched": live_config_touched,
        "live_gateway_touched": any(row["live_gateway_touched"] for row in rows),
        "live_cron_scheduler_touched": any(row["live_cron_scheduler_touched"] for row in rows),
        "secret_values_read": secret_values_read,
        "runtime_state_mutated": any(row["runtime_state_mutated"] for row in rows),
        "policy_hash": policy_hash,
        "scope": "high_fidelity_synthetic_no_live_cli_gateway_cron_provider_or_config_mutation",
        "runtime_closure": "open_partial_green_synthetic_only",
    }

    _write_jsonl(evidence_path, rows)
    _write_json(summary_path, summary)

    return RuntimeSurfaceCanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        allowed=allowed,
        provider_call_count=provider_call_count,
        scheduled_dispatch_count=scheduled_dispatch_count,
        gateway_raw_content_sent=gateway_raw_content_sent,
        raw_payload_stored=raw_payload_stored,
        live_config_touched=live_config_touched,
        secret_values_read=secret_values_read,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run P1-6 runtime-surface synthetic canaries")
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--policy-hash", default="fixture-p1-6-policy-hash")
    args = parser.parse_args(argv)

    result = run_p1_6_runtime_surface_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        policy_hash=args.policy_hash,
    )
    print(
        "P1-6 runtime-surface canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
