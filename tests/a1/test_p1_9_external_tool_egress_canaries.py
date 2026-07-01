"""P1-9 external tool/web/browser/terminal egress envelope canary proof.

High-fidelity synthetic only: no live tool dispatch, web/browser/terminal
execution, provider dispatch, profile/provider config mutation, or secret inspection.
"""

import json
from pathlib import Path

from hermes_cli.p1_9_external_tool_egress_canaries import run_p1_9_external_tool_egress_canaries


EXPECTED_RULES = {
    "P1-9-WEB-T01": "w5.p1_9.egress.web_c2_external_denied",
    "P1-9-BROWSER-T02": "w5.p1_9.egress.browser_c2_external_denied",
    "P1-9-TERMINAL-T03": "w5.p1_9.egress.terminal_network_denied",
    "P1-9-MISSING-ENVELOPE-T04": "w5.p1_9.egress.required_envelope_missing",
    "P1-9-C0-T05": "w5.p1_9.egress.c0_fake_tool_adapter_allowed",
}

REQUIRED_FIELDS = {
    "case_id",
    "decision",
    "rule_id",
    "surface",
    "classification",
    "classification_source",
    "correlation_id",
    "tool_call_id",
    "resolver_decision_id",
    "payload_capture_id",
    "requested_tool",
    "requested_sink_class",
    "requested_egress_target_digest",
    "payload_digest",
    "tool_args_digest",
    "allowed_sink_classes_digest",
    "dispatch_denied_before_tool",
    "live_tool_dispatch_count",
    "fake_tool_adapter_call_count",
    "web_call_count",
    "browser_action_count",
    "terminal_exec_count",
    "provider_call_count",
    "live_config_touched",
    "secret_values_read",
    "raw_payload_stored",
    "raw_tool_args_stored",
    "redaction_class",
}


def _rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p1_9_external_tool_egress_envelopes_deny_c2_nonlocal_sinks(tmp_path):
    result = run_p1_9_external_tool_egress_canaries(
        evidence_dir=tmp_path / "evidence",
        profile_id="architect-fixture",
        policy_hash="fixture-p1-9-policy-hash",
    )

    assert result.total == 5
    assert result.denied == 4
    assert result.allowed == 1
    assert result.live_tool_dispatch_count == 0
    assert result.fake_tool_adapter_call_count == 1
    assert result.web_call_count == 0
    assert result.browser_action_count == 0
    assert result.terminal_exec_count == 0
    assert result.provider_call_count == 0
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert result.raw_payload_stored is False
    assert result.raw_tool_args_stored is False

    evidence_path = Path(result.evidence_path)
    summary_path = Path(result.summary_path)
    assert evidence_path.exists()
    assert summary_path.exists()

    rows = _rows(evidence_path)
    assert [row["case_id"] for row in rows] == [
        "P1-9-WEB-T01",
        "P1-9-BROWSER-T02",
        "P1-9-TERMINAL-T03",
        "P1-9-MISSING-ENVELOPE-T04",
        "P1-9-C0-T05",
    ]

    for row in rows:
        assert REQUIRED_FIELDS.issubset(row.keys())
        assert row["rule_id"] == EXPECTED_RULES[row["case_id"]]
        assert row["profile_id"] == "architect-fixture"
        assert row["policy_hash"] == "fixture-p1-9-policy-hash"
        assert row["classification_source"] in {"hl_aos_frozen_tool_envelope", "unclassified"}
        assert row["correlation_id"].startswith("p1-9-correlation-")
        assert row["tool_call_id"].startswith(row["correlation_id"] + ":tool:")
        assert row["resolver_decision_id"].startswith("p1-9-resolver-")
        assert row["payload_capture_id"].startswith("p1-9-payload-")
        assert row["requested_egress_target_digest"].startswith("sha256:")
        assert row["payload_digest"].startswith("sha256:")
        assert row["tool_args_digest"].startswith("sha256:")
        assert row["allowed_sink_classes_digest"].startswith("sha256:")
        assert row["live_tool_dispatch_count"] == 0
        assert row["web_call_count"] == 0
        assert row["browser_action_count"] == 0
        assert row["terminal_exec_count"] == 0
        assert row["provider_call_count"] == 0
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_payload_stored"] is False
        assert row["raw_tool_args_stored"] is False
        assert row["redaction_class"] == "digest_only"

    by_case = {row["case_id"]: row for row in rows}

    web = by_case["P1-9-WEB-T01"]
    assert web["decision"] == "deny"
    assert web["surface"] == "synthetic_tool_web"
    assert web["classification"] == "C2_LOCAL_ONLY"
    assert web["requested_tool"] == "web_search"
    assert web["requested_sink_class"] == "external_web"
    assert web["dispatch_denied_before_tool"] is True
    assert web["fake_tool_adapter_call_count"] == 0

    browser = by_case["P1-9-BROWSER-T02"]
    assert browser["decision"] == "deny"
    assert browser["surface"] == "synthetic_tool_browser"
    assert browser["classification"] == "C2_LOCAL_ONLY"
    assert browser["requested_tool"] == "browser_navigate"
    assert browser["requested_sink_class"] == "external_browser"
    assert browser["dispatch_denied_before_tool"] is True

    terminal = by_case["P1-9-TERMINAL-T03"]
    assert terminal["decision"] == "deny"
    assert terminal["surface"] == "synthetic_tool_terminal"
    assert terminal["classification"] == "C2_LOCAL_ONLY"
    assert terminal["requested_tool"] == "terminal"
    assert terminal["requested_sink_class"] == "terminal_network_egress"
    assert terminal["network_intent_detected"] is True
    assert terminal["dispatch_denied_before_tool"] is True

    missing = by_case["P1-9-MISSING-ENVELOPE-T04"]
    assert missing["decision"] == "deny"
    assert missing["classification"] == "UNKNOWN"
    assert missing["classification_source"] == "unclassified"
    assert missing["missing_required_fields"] == ["classification", "payload_digest", "requested_sink_class"]
    assert missing["dispatch_denied_before_tool"] is True

    positive = by_case["P1-9-C0-T05"]
    assert positive["decision"] == "allow"
    assert positive["classification"] == "C0_PUBLIC"
    assert positive["requested_tool"] == "web_search"
    assert positive["requested_sink_class"] == "external_web"
    assert positive["dispatch_denied_before_tool"] is False
    assert positive["fake_tool_adapter_call_count"] == 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["case_ids"] == [
        "P1-9-WEB-T01",
        "P1-9-BROWSER-T02",
        "P1-9-TERMINAL-T03",
        "P1-9-MISSING-ENVELOPE-T04",
        "P1-9-C0-T05",
    ]
    assert summary["total"] == 5
    assert summary["denied"] == 4
    assert summary["allowed"] == 1
    assert summary["live_tool_dispatch_count"] == 0
    assert summary["fake_tool_adapter_call_count"] == 1
    assert summary["web_call_count"] == 0
    assert summary["browser_action_count"] == 0
    assert summary["terminal_exec_count"] == 0
    assert summary["provider_call_count"] == 0
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_payload_stored"] is False
    assert summary["raw_tool_args_stored"] is False
    assert summary["runtime_closure"] == "open_partial_green_synthetic_only"

    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "confidential tool raw payload" not in evidence_text
    assert "SECRET" not in evidence_text
