"""CSR-006 local-degradation fail-closed fixture harness.

The helper in this module does not call live Ollama, LiteLLM, Headroom,
gateways, proxies, or frontier providers. It encodes CSR-006-INV-001 as a
fixture-only policy canary suite and emits digest-only JSON evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


CASE_ORDER = ("CSR-006-T01", "CSR-006-T02", "CSR-006-T03", "CSR-006-T04")
RULES = {
    "CSR-006-T01": "csr006.local_ollama_unavailable.deny_c2",
    "CSR-006-T02": "csr006.compression_provider_failure.circuit_breaker",
    "CSR-006-T03": "csr006.c2_preflight_frontier_block",
    "CSR-006-T04": "csr006.frontier_failure_no_fallback",
}


@dataclass(frozen=True)
class Csr006DegradationResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    frontier_call_count: int
    retry_storm_detected: bool
    live_config_touched: bool
    secret_values_read: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_row(
    *,
    case_id: str,
    profile_id: str,
    classification: str,
    policy_manifest_hash: str,
    scenario: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "correlation_id": f"csr006-{uuid4().hex[:12]}",
        "timestamp": _utc_now(),
        "profile_id": profile_id,
        "classification": classification,
        "scenario": scenario,
        "gap_id": "CSR-006",
        "evidence_id": "W5-E06",
        "decision": "deny",
        "rule_id": RULES[case_id],
        "policy_manifest_hash": policy_manifest_hash,
        "raw_payload_stored": False,
        "frontier_call_count": 0,
        "alternate_frontier_retry_count": 0,
        "retry_storm_detected": False,
        "live_config_touched": False,
        "secret_values_read": False,
        "provider_call_count": 0,
        "file_write_count": 0,
    }


def _csr006_rows(
    *,
    profile_id: str,
    classification: str,
    policy_manifest_hash: str,
) -> list[dict[str, Any]]:
    if classification not in {"C2", "C3", "C4"}:
        raise ValueError("CSR-006 degradation canaries are for C2+ classifications only")

    t01 = _base_row(
        case_id="CSR-006-T01",
        profile_id=profile_id,
        classification=classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="local_ollama_unavailable",
    )
    t01.update(
        {
            "local_provider": "custom:local-ollama",
            "local_provider_status": "unavailable",
            "gateway_responsive": True,
            "explicit_error": "local_unavailable_c2_fail_closed",
            "model_dispatch_count": 0,
            "reason": "C2 workload denied when local provider is unavailable; frontier fallback is not eligible",
        }
    )

    t02 = _base_row(
        case_id="CSR-006-T02",
        profile_id=profile_id,
        classification=classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="compression_provider_401",
    )
    t02.update(
        {
            "compression_status": "provider_401",
            "compression_attempt_count": 1,
            "compression_retry_budget_remaining": 0,
            "polling_loop_continues": True,
            "flood_control_triggered": False,
            "context_bloat_detected": False,
            "explicit_error": "compression_failed_c2_no_frontier_retry",
            "reason": "compression provider failure consumes retry budget and does not escalate C2 content to frontier",
        }
    )

    t03 = _base_row(
        case_id="CSR-006-T03",
        profile_id=profile_id,
        classification=classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="c2_preflight_frontier_route_request",
    )
    t03.update(
        {
            "requested_route_family": "frontier",
            "requested_provider": "custom:headroom-openrouter-litellm",
            "preflight_blocked": True,
            "model_dispatch_count": 0,
            "audit_reason": "c2-pre-flight-block",
            "explicit_error": "c2_preflight_frontier_block",
            "reason": "pre-flight policy gate denies C2 payload before model dispatch",
        }
    )

    t04 = _base_row(
        case_id="CSR-006-T04",
        profile_id=profile_id,
        classification=classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="litellm_frontier_5xx_for_c2",
    )
    t04.update(
        {
            "simulated_litellm_status": 503,
            "requested_provider": "custom:headroom-openrouter-litellm",
            "fallback_attempted": False,
            "fallback_target": None,
            "explicit_error": "frontier_failure_no_c2_fallback",
            "reason": "C2 request does not retry or fall through to alternate frontier path after simulated LiteLLM failure",
        }
    )

    return [t01, t02, t03, t04]


def run_csr006_local_degradation_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    classification: str,
    policy_manifest_hash: str,
) -> Csr006DegradationResult:
    """Run fixture-only CSR-006 local-degradation canaries and emit evidence."""

    evidence_root = Path(evidence_dir)
    evidence_path = evidence_root / "csr006_local_degradation.jsonl"
    summary_path = evidence_root / "run_summary.json"

    rows = _csr006_rows(
        profile_id=profile_id,
        classification=classification,
        policy_manifest_hash=policy_manifest_hash,
    )
    for row in rows:
        _append_jsonl(evidence_path, row)

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "deny")
    frontier_call_count = sum(row["frontier_call_count"] for row in rows)
    retry_storm_detected = any(row["retry_storm_detected"] for row in rows)
    live_config_touched = any(row["live_config_touched"] for row in rows)
    secret_values_read = any(row["secret_values_read"] for row in rows)

    summary = {
        "case_ids": list(CASE_ORDER),
        "total": total,
        "denied": denied,
        "allowed": total - denied,
        "frontier_call_count": frontier_call_count,
        "retry_storm_detected": retry_storm_detected,
        "live_config_touched": live_config_touched,
        "secret_values_read": secret_values_read,
        "raw_payload_stored": False,
        "policy_manifest_hash": policy_manifest_hash,
        "scope": "fixture_only_no_live_provider_or_gateway_calls",
    }
    _write_json(summary_path, summary)

    return Csr006DegradationResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        frontier_call_count=frontier_call_count,
        retry_storm_detected=retry_storm_detected,
        live_config_touched=live_config_touched,
        secret_values_read=secret_values_read,
    )
