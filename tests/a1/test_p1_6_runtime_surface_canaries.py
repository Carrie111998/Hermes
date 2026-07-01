"""P1-6 CLI/gateway/cron runtime-surface canary proof.

High-fidelity synthetic canaries only: no live CLI invocation, gateway send,
cron scheduler mutation, provider call, config mutation, or secret inspection.
The harness simulates realistic per-surface envelopes and proves the shared
surface adapter -> guard/resolver -> payload capture -> dispatch gate shape.
"""

import json
from pathlib import Path

from hermes_cli.p1_6_runtime_surface_canaries import run_p1_6_runtime_surface_canaries


EXPECTED_RULES = {
    ("P1-6-CLI-T01", "cli"): "w5.p1_6.cli.c2_frontier_denied",
    ("P1-6-GATEWAY-T02", "gateway_telegram"): "w5.p1_6.gateway.approval_ref_required",
    ("P1-6-CRON-T03", "cron"): "w5.p1_6.cron.disallowed_frontier_sink_denied",
    ("P1-6-CORRELATION-T04", "cli"): "w5.p1_6.envelope.required_field_missing",
    ("P1-6-CORRELATION-T04", "gateway_telegram"): "w5.p1_6.envelope.required_field_missing",
    ("P1-6-CORRELATION-T04", "cron"): "w5.p1_6.envelope.required_field_missing",
    ("P1-6-C0-T05", "cli"): "w5.p1_6.c0.local_fake_adapter_allowed",
}

REQUIRED_EVIDENCE_FIELDS = {
    "case_id",
    "surface",
    "decision",
    "rule_id",
    "classification",
    "classification_source",
    "requested_target",
    "resolved_target",
    "correlation_id",
    "request_id",
    "resolver_decision_id",
    "payload_digest",
    "redaction_class",
    "provider_call_count",
    "gateway_raw_content_sent",
    "raw_payload_stored",
    "live_config_touched",
    "secret_values_read",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p1_6_cli_gateway_cron_surfaces_share_guard_resolver_payload_shape(tmp_path):
    result = run_p1_6_runtime_surface_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        policy_hash="fixture-p1-6-policy-hash",
    )

    assert result.total == 7
    assert result.denied == 6
    assert result.allowed == 1
    assert result.provider_call_count == 0
    assert result.scheduled_dispatch_count == 0
    assert result.gateway_raw_content_sent is False
    assert result.raw_payload_stored is False
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert Path(result.evidence_path).exists()
    assert Path(result.summary_path).exists()

    rows = _rows(Path(result.evidence_path))
    assert [(row["case_id"], row["surface"]) for row in rows] == [
        ("P1-6-CLI-T01", "cli"),
        ("P1-6-GATEWAY-T02", "gateway_telegram"),
        ("P1-6-CRON-T03", "cron"),
        ("P1-6-CORRELATION-T04", "cli"),
        ("P1-6-CORRELATION-T04", "gateway_telegram"),
        ("P1-6-CORRELATION-T04", "cron"),
        ("P1-6-C0-T05", "cli"),
    ]

    for row in rows:
        assert REQUIRED_EVIDENCE_FIELDS.issubset(row.keys())
        assert row["rule_id"] == EXPECTED_RULES[(row["case_id"], row["surface"])]
        assert row["policy_hash"] == "fixture-p1-6-policy-hash"
        assert row["classification_source"] in {"hl_aos_frozen_surface_metadata", "surface_envelope_missing_required_field"}
        assert row["payload_digest"].startswith("sha256:")
        assert row["redaction_class"] == "digest_only"
        assert row["guard_decision_id"].startswith("guard-")
        assert row["resolver_decision_id"].startswith("resolver-")
        assert row["payload_capture_id"].startswith("payload-")
        assert row["decision_shape"] == ["surface_adapter", "guard_resolver", "payload_capture", "dispatch_gate"]
        assert row["provider_call_count"] == 0
        assert row["gateway_raw_content_sent"] is False
        assert row["raw_payload_stored"] is False
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["live_gateway_touched"] is False
        assert row["live_cron_scheduler_touched"] is False
        assert row["runtime_state_mutated"] is False

    by_case_surface = {(row["case_id"], row["surface"]): row for row in rows}

    cli = by_case_surface[("P1-6-CLI-T01", "cli")]
    assert cli["decision"] == "deny"
    assert cli["classification"] == "C2_LOCAL_ONLY"
    assert cli["requested_target"] == "frontier"
    assert cli["resolved_target"] is None
    assert cli["dispatch_denied_before_provider"] is True
    assert cli["provider_call_count"] == 0

    gateway = by_case_surface[("P1-6-GATEWAY-T02", "gateway_telegram")]
    assert gateway["decision"] == "deny"
    assert gateway["classification"] == "C2_LOCAL_ONLY"
    assert gateway["signed_approval_ref_present"] is False
    assert gateway["gateway_raw_content_sent"] is False
    assert gateway["raw_payload_stored"] is False
    assert gateway["provider_call_count"] == 0

    cron = by_case_surface[("P1-6-CRON-T03", "cron")]
    assert cron["decision"] == "deny"
    assert cron["classification"] == "C2_LOCAL_ONLY"
    assert cron["job_id"] == "cron-job-p1-6-c2-frontier"
    assert cron["requested_sink_path"] == "frontier_model_dispatch"
    assert cron["scheduled_dispatch_count"] == 0
    assert cron["provider_call_count"] == 0

    t04_rows = [row for row in rows if row["case_id"] == "P1-6-CORRELATION-T04"]
    assert {row["surface"] for row in t04_rows} == {"cli", "gateway_telegram", "cron"}
    assert all(row["decision"] == "deny" for row in t04_rows)
    assert all(row["fail_closed_missing_required_field"] is True for row in t04_rows)
    assert {row["missing_envelope_field"] for row in t04_rows} == {"request_id", "payload_digest", "job_id"}

    positive = by_case_surface[("P1-6-C0-T05", "cli")]
    assert positive["decision"] == "allow"
    assert positive["classification"] == "C0_PUBLIC"
    assert positive["requested_target"] == "local_fake_provider"
    assert positive["resolved_target"] == "local_fake_provider"
    assert positive["fake_local_provider_adapter_call_count"] == 1
    assert positive["provider_call_count"] == 0

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["case_ids"] == ["P1-6-CLI-T01", "P1-6-GATEWAY-T02", "P1-6-CRON-T03", "P1-6-CORRELATION-T04", "P1-6-C0-T05"]
    assert summary["surfaces"] == ["cli", "gateway_telegram", "cron"]
    assert summary["total"] == 7
    assert summary["denied"] == 6
    assert summary["allowed"] == 1
    assert summary["provider_call_count"] == 0
    assert summary["scheduled_dispatch_count"] == 0
    assert summary["gateway_raw_content_sent"] is False
    assert summary["raw_payload_stored"] is False
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["scope"] == "high_fidelity_synthetic_no_live_cli_gateway_cron_provider_or_config_mutation"

    evidence_text = Path(result.evidence_path).read_text(encoding="utf-8")
    assert "C2 CLI raw runtime-boundary prompt" not in evidence_text
    assert "Telegram confidential body" not in evidence_text
    assert "Stored C2 cron prompt" not in evidence_text
