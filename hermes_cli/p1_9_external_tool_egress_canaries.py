"""P1-9 high-fidelity synthetic external tool egress canaries.

This harness simulates external tool/web/browser/terminal egress envelopes
without invoking live tools, web/browser/terminal execution, providers, secrets,
or live provider/profile/runtime configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


CASE_ORDER = (
    "P1-9-WEB-T01",
    "P1-9-BROWSER-T02",
    "P1-9-TERMINAL-T03",
    "P1-9-MISSING-ENVELOPE-T04",
    "P1-9-C0-T05",
)

RULES = {
    "web_c2": "w5.p1_9.egress.web_c2_external_denied",
    "browser_c2": "w5.p1_9.egress.browser_c2_external_denied",
    "terminal_network": "w5.p1_9.egress.terminal_network_denied",
    "missing_envelope": "w5.p1_9.egress.required_envelope_missing",
    "c0_allowed": "w5.p1_9.egress.c0_fake_tool_adapter_allowed",
}


@dataclass(frozen=True)
class ExternalToolEgressCanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    live_tool_dispatch_count: int
    fake_tool_adapter_call_count: int
    web_call_count: int
    browser_action_count: int
    terminal_exec_count: int
    provider_call_count: int
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool
    raw_tool_args_stored: bool


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
    requested_tool: str,
    requested_sink_class: str,
    requested_egress_target: str,
    allowed_sink_classes: list[str],
    missing_required_fields: list[str] | None = None,
    network_intent_detected: bool = False,
    fake_tool_adapter_call_count: int = 0,
) -> dict[str, Any]:
    correlation_id = _stable_id("p1-9-correlation", case_id)
    tool_call_id = f"{correlation_id}:tool:{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:8]}"
    resolver_decision_id = _stable_id("p1-9-resolver", case_id, requested_tool)
    payload_capture_id = _stable_id("p1-9-payload", case_id, requested_sink_class)
    tool_args_fixture = {
        "case_id": case_id,
        "tool": requested_tool,
        "target_digest_input": requested_egress_target,
        "raw_args": "digest-only synthetic tool args fixture",
    }
    payload_fixture = {
        "case_id": case_id,
        "classification": classification,
        "tool_call_id": tool_call_id,
        "payload": "digest-only synthetic outbound tool payload",
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
        "tool_call_id": tool_call_id,
        "resolver_decision_id": resolver_decision_id,
        "payload_capture_id": payload_capture_id,
        "requested_tool": requested_tool,
        "requested_sink_class": requested_sink_class,
        "requested_egress_target_digest": _sha256_text(requested_egress_target),
        "payload_digest": _digest_obj(payload_fixture),
        "tool_args_digest": _digest_obj(tool_args_fixture),
        "allowed_sink_classes_digest": _digest_obj(allowed_sink_classes),
        "allowed_sink_classes": allowed_sink_classes,
        "missing_required_fields": missing_required_fields or [],
        "network_intent_detected": network_intent_detected,
        "dispatch_denied_before_tool": decision == "deny",
        "live_tool_dispatch_count": 0,
        "fake_tool_adapter_call_count": fake_tool_adapter_call_count,
        "web_call_count": 0,
        "browser_action_count": 0,
        "terminal_exec_count": 0,
        "provider_call_count": 0,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "raw_tool_args_stored": False,
        "redaction_class": "digest_only",
        "scope": "high_fidelity_synthetic_no_live_tool_web_browser_terminal_provider_or_config_calls",
    }


def _rows() -> list[dict[str, Any]]:
    local_only = ["local_file_read", "local_analysis"]
    public_external = ["external_web", "local_file_read", "local_analysis"]
    return [
        _base_row(
            case_id="P1-9-WEB-T01",
            decision="deny",
            rule_id=RULES["web_c2"],
            reason="C2/local-only request attempted external web_search egress",
            surface="synthetic_tool_web",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_tool_envelope",
            requested_tool="web_search",
            requested_sink_class="external_web",
            requested_egress_target="https://example.invalid/search?q=digest-only-fixture",
            allowed_sink_classes=local_only,
        ),
        _base_row(
            case_id="P1-9-BROWSER-T02",
            decision="deny",
            rule_id=RULES["browser_c2"],
            reason="C2/local-only request attempted browser navigation to external sink",
            surface="synthetic_tool_browser",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_tool_envelope",
            requested_tool="browser_navigate",
            requested_sink_class="external_browser",
            requested_egress_target="https://example.invalid/browser-fixture",
            allowed_sink_classes=local_only,
        ),
        _base_row(
            case_id="P1-9-TERMINAL-T03",
            decision="deny",
            rule_id=RULES["terminal_network"],
            reason="C2/local-only terminal command carried network egress intent",
            surface="synthetic_tool_terminal",
            classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_tool_envelope",
            requested_tool="terminal",
            requested_sink_class="terminal_network_egress",
            requested_egress_target="curl https://example.invalid/digest-only-fixture",
            allowed_sink_classes=local_only,
            network_intent_detected=True,
        ),
        _base_row(
            case_id="P1-9-MISSING-ENVELOPE-T04",
            decision="deny",
            rule_id=RULES["missing_envelope"],
            reason="external tool envelope missing classification, payload digest, or sink class",
            surface="synthetic_tool_web",
            classification="UNKNOWN",
            classification_source="unclassified",
            requested_tool="web_search",
            requested_sink_class="UNKNOWN",
            requested_egress_target="https://example.invalid/missing-envelope-fixture",
            allowed_sink_classes=local_only,
            missing_required_fields=["classification", "payload_digest", "requested_sink_class"],
        ),
        _base_row(
            case_id="P1-9-C0-T05",
            decision="allow",
            rule_id=RULES["c0_allowed"],
            reason="C0/public external web request allowed only through fake tool adapter",
            surface="synthetic_tool_web",
            classification="C0_PUBLIC",
            classification_source="hl_aos_frozen_tool_envelope",
            requested_tool="web_search",
            requested_sink_class="external_web",
            requested_egress_target="https://example.invalid/public-fixture",
            allowed_sink_classes=public_external,
            fake_tool_adapter_call_count=1,
        ),
    ]


def run_p1_9_external_tool_egress_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    policy_hash: str,
) -> ExternalToolEgressCanaryResult:
    evidence_dir = Path(evidence_dir)
    rows = _rows()
    for row in rows:
        row["profile_id"] = profile_id
        row["policy_hash"] = policy_hash

    evidence_path = evidence_dir / "p1_9_external_tool_egress_evidence.jsonl"
    summary_path = evidence_dir / "p1_9_external_tool_egress_summary.json"
    _write_jsonl(evidence_path, rows)

    summary = {
        "case_ids": [row["case_id"] for row in rows],
        "total": len(rows),
        "denied": sum(row["decision"] == "deny" for row in rows),
        "allowed": sum(row["decision"] == "allow" for row in rows),
        "surfaces": sorted({row["surface"] for row in rows}),
        "rule_ids": [row["rule_id"] for row in rows],
        "live_tool_dispatch_count": sum(row["live_tool_dispatch_count"] for row in rows),
        "fake_tool_adapter_call_count": sum(row["fake_tool_adapter_call_count"] for row in rows),
        "web_call_count": sum(row["web_call_count"] for row in rows),
        "browser_action_count": sum(row["browser_action_count"] for row in rows),
        "terminal_exec_count": sum(row["terminal_exec_count"] for row in rows),
        "provider_call_count": sum(row["provider_call_count"] for row in rows),
        "live_config_touched": any(row["live_config_touched"] for row in rows),
        "secret_values_read": any(row["secret_values_read"] for row in rows),
        "raw_payload_stored": any(row["raw_payload_stored"] for row in rows),
        "raw_tool_args_stored": any(row["raw_tool_args_stored"] for row in rows),
        "runtime_closure": "open_partial_green_synthetic_only",
    }
    _write_json(summary_path, summary)

    return ExternalToolEgressCanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=summary["total"],
        denied=summary["denied"],
        allowed=summary["allowed"],
        live_tool_dispatch_count=summary["live_tool_dispatch_count"],
        fake_tool_adapter_call_count=summary["fake_tool_adapter_call_count"],
        web_call_count=summary["web_call_count"],
        browser_action_count=summary["browser_action_count"],
        terminal_exec_count=summary["terminal_exec_count"],
        provider_call_count=summary["provider_call_count"],
        live_config_touched=summary["live_config_touched"],
        secret_values_read=summary["secret_values_read"],
        raw_payload_stored=summary["raw_payload_stored"],
        raw_tool_args_stored=summary["raw_tool_args_stored"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--policy-hash", default="fixture-p1-9-policy-hash")
    args = parser.parse_args()

    result = run_p1_9_external_tool_egress_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        policy_hash=args.policy_hash,
    )
    print(
        "P1-9 external tool egress canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )


if __name__ == "__main__":
    main()
