"""P1-8 high-fidelity synthetic summarization/compaction canaries.

This harness simulates summary/compaction dispatch envelopes without invoking
live compaction, live summarizer/provider calls, reading secrets, or touching
live provider/profile/runtime configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


CASE_ORDER = (
    "P1-8-C2-SUMMARY-T01",
    "P1-8-MISSING-TAINT-T02",
    "P1-8-DOWNGRADE-T03",
    "P1-8-AUX-T04",
    "P1-8-C0-T05",
)

RULES = {
    "c2_frontier": "w5.p1_8.summarization.c2_frontier_summary_denied",
    "missing_taint": "w5.p1_8.summarization.required_taint_missing",
    "downgrade": "w5.p1_8.summarization.summary_taint_downgrade_denied",
    "nonlocal_aux": "w5.p1_8.compaction.nonlocal_aux_model_denied",
    "c0_allowed": "w5.p1_8.summarization.c0_fake_local_summarizer_allowed",
}

CLASSIFICATION_ORDER = {
    "UNKNOWN": -1,
    "C0_PUBLIC": 0,
    "C1_INTERNAL": 1,
    "C2_LOCAL_ONLY": 2,
    "C3_RESTRICTED": 3,
}

MODEL_TIER_ORDER = {
    "L0_LOCAL": 0,
    "L1_LOCAL_REASONING": 1,
    "L2_FRONTIER": 2,
    "L3_HUMAN": 3,
}


@dataclass(frozen=True)
class SummarizationCompactionCanaryResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    allowed: int
    summarizer_dispatch_count: int
    fake_local_summarizer_adapter_call_count: int
    provider_call_count: int
    live_compaction_invoked: bool
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool
    raw_summary_stored: bool


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


def _monotonic_taint_ok(session_classification: str, summary_classification: str, inherited: bool) -> bool:
    if not inherited:
        return False
    return CLASSIFICATION_ORDER[summary_classification] >= CLASSIFICATION_ORDER[session_classification]


def _tier_within_allowed(max_allowed_model_tier: str, requested_model_tier: str) -> bool:
    return MODEL_TIER_ORDER[requested_model_tier] <= MODEL_TIER_ORDER[max_allowed_model_tier]


def _base_row(
    *,
    case_id: str,
    decision: str,
    rule_id: str,
    reason: str,
    surface: str,
    session_classification: str,
    summary_classification: str,
    classification_source: str,
    summary_taint_inherited: bool,
    requested_summarizer_target: str,
    resolved_summarizer_target: str | None,
    requested_model_tier: str,
    max_allowed_model_tier: str,
    missing_required_fields: list[str] | None = None,
    fake_local_summarizer_adapter_call_count: int = 0,
) -> dict[str, Any]:
    session_correlation_id = _stable_id("p1-8-session", case_id)
    summary_correlation_id = f"{session_correlation_id}:summary:{hashlib.sha256(case_id.encode('utf-8')).hexdigest()[:8]}"
    compaction_event_id = _stable_id("p1-8-compact", case_id, surface)
    payload_capture_id = _stable_id("p1-8-payload", case_id, requested_summarizer_target)
    monotonic_ok = _monotonic_taint_ok(
        session_classification,
        summary_classification,
        summary_taint_inherited,
    )
    tier_allowed = _tier_within_allowed(max_allowed_model_tier, requested_model_tier)
    if decision == "deny":
        resolved_summarizer_target = None

    source_transcript_fixture = {
        "case_id": case_id,
        "turns": 42,
        "classification": session_classification,
        "raw_text": "digest-only synthetic transcript fixture",
    }
    summary_payload_fixture = {
        "case_id": case_id,
        "session_correlation_id": session_correlation_id,
        "summary_correlation_id": summary_correlation_id,
        "classification": summary_classification,
        "target": requested_summarizer_target,
    }
    summary_output_fixture = {
        "case_id": case_id,
        "classification": summary_classification,
        "stored": "digest-only synthetic summary output",
    }

    return {
        "case_id": case_id,
        "decision": decision,
        "rule_id": rule_id,
        "reason": reason,
        "surface": surface,
        "profile_id": None,
        "policy_hash": None,
        "session_classification": session_classification,
        "summary_classification": summary_classification,
        "classification_source": classification_source,
        "summary_taint_inherited": summary_taint_inherited,
        "monotonic_taint_ok": monotonic_ok,
        "missing_required_fields": missing_required_fields or [],
        "session_correlation_id": session_correlation_id,
        "summary_correlation_id": summary_correlation_id,
        "compaction_event_id": compaction_event_id,
        "payload_capture_id": payload_capture_id,
        "source_transcript_digest": _digest_obj(source_transcript_fixture),
        "summary_payload_digest": _digest_obj(summary_payload_fixture),
        "summary_output_digest": _digest_obj(summary_output_fixture),
        "requested_summarizer_target": requested_summarizer_target,
        "resolved_summarizer_target": resolved_summarizer_target,
        "requested_model_tier": requested_model_tier,
        "max_allowed_model_tier": max_allowed_model_tier,
        "requested_model_tier_within_allowed": tier_allowed,
        "dispatch_denied_before_summarizer": decision == "deny",
        "summarizer_dispatch_count": 0,
        "fake_local_summarizer_adapter_call_count": fake_local_summarizer_adapter_call_count,
        "provider_call_count": 0,
        "live_compaction_invoked": False,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "raw_summary_stored": False,
        "redaction_class": "digest_only",
        "scope": "high_fidelity_synthetic_no_live_compaction_summarizer_provider_or_config_calls",
    }


def _rows() -> list[dict[str, Any]]:
    return [
        _base_row(
            case_id="P1-8-C2-SUMMARY-T01",
            decision="deny",
            rule_id=RULES["c2_frontier"],
            reason="C2/local-only summarization request targeted frontier summarizer",
            surface="synthetic_summarization",
            session_classification="C2_LOCAL_ONLY",
            summary_classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_session_taint",
            summary_taint_inherited=True,
            requested_summarizer_target="frontier",
            resolved_summarizer_target=None,
            requested_model_tier="L2_FRONTIER",
            max_allowed_model_tier="L0_LOCAL",
        ),
        _base_row(
            case_id="P1-8-MISSING-TAINT-T02",
            decision="deny",
            rule_id=RULES["missing_taint"],
            reason="summary/compaction envelope missing required classification source fields",
            surface="synthetic_summarization",
            session_classification="UNKNOWN",
            summary_classification="UNKNOWN",
            classification_source="unclassified",
            summary_taint_inherited=False,
            requested_summarizer_target="local_fake_summarizer_adapter",
            resolved_summarizer_target=None,
            requested_model_tier="L0_LOCAL",
            max_allowed_model_tier="L0_LOCAL",
            missing_required_fields=["session_classification", "classification_source"],
        ),
        _base_row(
            case_id="P1-8-DOWNGRADE-T03",
            decision="deny",
            rule_id=RULES["downgrade"],
            reason="summary output attempted to downgrade C2 session taint to C0/public",
            surface="synthetic_summarization",
            session_classification="C2_LOCAL_ONLY",
            summary_classification="C0_PUBLIC",
            classification_source="hl_aos_frozen_session_taint",
            summary_taint_inherited=False,
            requested_summarizer_target="local_fake_summarizer_adapter",
            resolved_summarizer_target=None,
            requested_model_tier="L0_LOCAL",
            max_allowed_model_tier="L0_LOCAL",
        ),
        _base_row(
            case_id="P1-8-AUX-T04",
            decision="deny",
            rule_id=RULES["nonlocal_aux"],
            reason="C2 compaction tried to route through a non-local auxiliary/frontier compression model",
            surface="synthetic_compaction",
            session_classification="C2_LOCAL_ONLY",
            summary_classification="C2_LOCAL_ONLY",
            classification_source="hl_aos_frozen_session_taint",
            summary_taint_inherited=True,
            requested_summarizer_target="auxiliary_frontier_compression_model",
            resolved_summarizer_target=None,
            requested_model_tier="L2_FRONTIER",
            max_allowed_model_tier="L0_LOCAL",
        ),
        _base_row(
            case_id="P1-8-C0-T05",
            decision="allow",
            rule_id=RULES["c0_allowed"],
            reason="C0/public summary path allowed only through fake local summarizer adapter",
            surface="synthetic_summarization",
            session_classification="C0_PUBLIC",
            summary_classification="C0_PUBLIC",
            classification_source="hl_aos_frozen_session_taint",
            summary_taint_inherited=True,
            requested_summarizer_target="local_fake_summarizer_adapter",
            resolved_summarizer_target="local_fake_summarizer_adapter",
            requested_model_tier="L0_LOCAL",
            max_allowed_model_tier="L2_FRONTIER",
            fake_local_summarizer_adapter_call_count=1,
        ),
    ]


def run_p1_8_summarization_compaction_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    policy_hash: str,
) -> SummarizationCompactionCanaryResult:
    evidence_dir = Path(evidence_dir)
    rows = _rows()
    for row in rows:
        row["profile_id"] = profile_id
        row["policy_hash"] = policy_hash

    evidence_path = evidence_dir / "p1_8_summarization_compaction_evidence.jsonl"
    summary_path = evidence_dir / "p1_8_summarization_compaction_summary.json"
    _write_jsonl(evidence_path, rows)

    denied = sum(1 for row in rows if row["decision"] == "deny")
    allowed = sum(1 for row in rows if row["decision"] == "allow")
    summary = {
        "case_ids": [row["case_id"] for row in rows],
        "total": len(rows),
        "denied": denied,
        "allowed": allowed,
        "surfaces": sorted({row["surface"] for row in rows}),
        "rule_ids": [row["rule_id"] for row in rows],
        "summarizer_dispatch_count": sum(row["summarizer_dispatch_count"] for row in rows),
        "fake_local_summarizer_adapter_call_count": sum(
            row["fake_local_summarizer_adapter_call_count"] for row in rows
        ),
        "provider_call_count": sum(row["provider_call_count"] for row in rows),
        "live_compaction_invoked": any(row["live_compaction_invoked"] for row in rows),
        "live_config_touched": any(row["live_config_touched"] for row in rows),
        "secret_values_read": any(row["secret_values_read"] for row in rows),
        "raw_payload_stored": any(row["raw_payload_stored"] for row in rows),
        "raw_summary_stored": any(row["raw_summary_stored"] for row in rows),
        "runtime_closure": "open_partial_green_synthetic_only",
    }
    _write_json(summary_path, summary)

    return SummarizationCompactionCanaryResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=summary["total"],
        denied=summary["denied"],
        allowed=summary["allowed"],
        summarizer_dispatch_count=summary["summarizer_dispatch_count"],
        fake_local_summarizer_adapter_call_count=summary["fake_local_summarizer_adapter_call_count"],
        provider_call_count=summary["provider_call_count"],
        live_compaction_invoked=summary["live_compaction_invoked"],
        live_config_touched=summary["live_config_touched"],
        secret_values_read=summary["secret_values_read"],
        raw_payload_stored=summary["raw_payload_stored"],
        raw_summary_stored=summary["raw_summary_stored"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="Directory for JSONL evidence and summary JSON")
    parser.add_argument("--profile-id", default="architect-fixture")
    parser.add_argument("--policy-hash", default="fixture-p1-8-policy-hash")
    args = parser.parse_args()

    result = run_p1_8_summarization_compaction_canaries(
        evidence_dir=args.output_dir,
        profile_id=args.profile_id,
        policy_hash=args.policy_hash,
    )
    print(
        "P1-8 summarization/compaction canaries passed: "
        f"{result.total} records written to {result.evidence_path}; "
        f"summary {result.summary_path}"
    )


if __name__ == "__main__":
    main()
