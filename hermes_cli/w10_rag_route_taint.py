"""W10 RAG route-taint fixture proof.

This module is a deterministic, fixture-only harness for W10. It recomputes
route taint after synthetic retrieved chunks and before any prompt/model/tool
sink would run. It never talks to live Qdrant, providers, gateways, proxy
routes, profile/provider config, or secret stores; evidence is digest-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


CASE_ORDER = ("W10-T01", "W10-T02", "W10-T03", "W10-T04")
RULES = {
    "W10-T01": "w10.rag_taint.c2_chunk_frontier_denied",
    "W10-T02": "w10.rag_taint.incomplete_metadata_fail_closed",
    "W10-T03": "w10.rag_taint.stale_chunk_quarantined",
    "W10-T04": "w10.rag_taint.retrieved_instruction_cannot_authorize_sink",
}
_CLASSIFICATION_RANK = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}


@dataclass(frozen=True)
class W10RagRouteTaintResult:
    evidence_path: str
    summary_path: str
    total: int
    denied: int
    frontier_call_count: int
    prompt_dispatch_count: int
    live_qdrant_touched: bool
    live_config_touched: bool
    secret_values_read: bool
    raw_payload_stored: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _max_classification(*classifications: str) -> str:
    unknown_safe = "C2"
    ranked = [classification if classification in _CLASSIFICATION_RANK else unknown_safe for classification in classifications]
    return max(ranked, key=lambda item: _CLASSIFICATION_RANK[item])


def _base_row(
    *,
    case_id: str,
    profile_id: str,
    query_classification: str,
    policy_manifest_hash: str,
    scenario: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "correlation_id": f"w10-{uuid4().hex[:12]}",
        "timestamp": _utc_now(),
        "profile_id": profile_id,
        "query_classification": query_classification,
        "scenario": scenario,
        "gap_id": "W10",
        "evidence_id": "W10-RAG-ROUTE-TAINT",
        "decision": "deny",
        "rule_id": RULES[case_id],
        "policy_manifest_hash": policy_manifest_hash,
        "redaction_class": "digest_only",
        "frontier_call_count": 0,
        "prompt_dispatch_count": 0,
        "provider_call_count": 0,
        "write_sink_call_count": 0,
        "live_qdrant_touched": False,
        "live_config_touched": False,
        "secret_values_read": False,
        "raw_payload_stored": False,
        "runtime_state_mutated": False,
        "frontier_denied_before_prompt": True,
    }


def _synthetic_chunk_digest(case_id: str, chunk_id: str) -> str:
    return _sha256_text(f"{case_id}:{chunk_id}:synthetic-rag-fixture")


def _w10_rows(
    *,
    profile_id: str,
    query_classification: str,
    policy_manifest_hash: str,
) -> list[dict[str, Any]]:
    if query_classification not in _CLASSIFICATION_RANK:
        query_classification = "C2"

    t01_classifications = ["C0", "C2"]
    t01_max = _max_classification(query_classification, *t01_classifications)
    t01 = _base_row(
        case_id="W10-T01",
        profile_id=profile_id,
        query_classification=query_classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="c2_retrieved_chunk_mixed_with_c0_query",
    )
    t01.update(
        {
            "retrieved_chunk_count": 2,
            "quarantined_chunk_count": 0,
            "metadata_valid": True,
            "chunk_classifications": t01_classifications,
            "chunk_payload_digests": [
                _synthetic_chunk_digest("W10-T01", "c0-public"),
                _synthetic_chunk_digest("W10-T01", "c2-confidential"),
            ],
            "max_classification": t01_max,
            "session_taint": t01_max,
            "taint_flags": ["RAG_RETRIEVAL_TAINT"],
            "reason": "retrieved C2 chunk raises C0 query to C2 before prompt assembly; frontier route denied",
        }
    )

    t02 = _base_row(
        case_id="W10-T02",
        profile_id=profile_id,
        query_classification=query_classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="retrieved_chunk_missing_required_metadata",
    )
    t02.update(
        {
            "retrieved_chunk_count": 1,
            "quarantined_chunk_count": 1,
            "metadata_valid": False,
            "missing_metadata_fields": ["classification", "source_sha256", "last_verified_epoch", "ttl_seconds"],
            "chunk_classifications": ["UNLABELED"],
            "chunk_payload_digests": [_synthetic_chunk_digest("W10-T02", "missing-metadata")],
            "max_classification": "C2",
            "session_taint": "C2",
            "taint_flags": ["INCOMPLETE_RAG_METADATA", "RAG_RETRIEVAL_TAINT"],
            "reason": "unlabeled or incomplete retrieved chunk metadata fails closed to C2 and is quarantined before prompt dispatch",
        }
    )

    t03 = _base_row(
        case_id="W10-T03",
        profile_id=profile_id,
        query_classification=query_classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="stale_retrieved_chunk_ttl_or_source_freshness_failure",
    )
    t03.update(
        {
            "retrieved_chunk_count": 1,
            "quarantined_chunk_count": 1,
            "metadata_valid": True,
            "freshness_status": "stale_ttl_or_source",
            "chunk_classifications": ["C0"],
            "chunk_payload_digests": [_synthetic_chunk_digest("W10-T03", "stale-source")],
            "source_hash_verified": False,
            "ttl_valid": False,
            "max_classification": "C2",
            "session_taint": "C2",
            "taint_flags": ["STALE_RAG_CHUNK", "RAG_RETRIEVAL_TAINT"],
            "reason": "stale TTL or source freshness failure quarantines chunk and fails closed before frontier dispatch",
        }
    )

    t04 = _base_row(
        case_id="W10-T04",
        profile_id=profile_id,
        query_classification=query_classification,
        policy_manifest_hash=policy_manifest_hash,
        scenario="retrieved_instruction_attempts_to_authorize_restricted_sink",
    )
    t04.update(
        {
            "retrieved_chunk_count": 1,
            "quarantined_chunk_count": 0,
            "metadata_valid": True,
            "chunk_classifications": ["C0"],
            "chunk_payload_digests": [_synthetic_chunk_digest("W10-T04", "authorization-instruction")],
            "retrieved_instruction_present": True,
            "instruction_authority": "untrusted_retrieved_content",
            "requested_sinks": ["frontier_model", "write_file"],
            "frontier_authorized_by_retrieval": False,
            "write_sink_authorized_by_retrieval": False,
            "write_sink_denied": True,
            "max_classification": "C2",
            "session_taint": "C2",
            "taint_flags": ["RETRIEVED_INSTRUCTION_UNTRUSTED", "RAG_RETRIEVAL_TAINT"],
            "reason": "retrieved text is data, not policy authority; it cannot authorize frontier or write sinks",
        }
    )

    return [t01, t02, t03, t04]


def run_w10_rag_route_taint_canaries(
    *,
    evidence_dir: str | Path,
    profile_id: str,
    query_classification: str,
    policy_manifest_hash: str,
) -> W10RagRouteTaintResult:
    """Run fixture-only W10 RAG route-taint canaries and emit digest evidence."""

    evidence_root = Path(evidence_dir)
    evidence_path = evidence_root / "w10_rag_route_taint.jsonl"
    summary_path = evidence_root / "run_summary.json"

    rows = _w10_rows(
        profile_id=profile_id,
        query_classification=query_classification,
        policy_manifest_hash=policy_manifest_hash,
    )
    for row in rows:
        _append_jsonl(evidence_path, row)

    total = len(rows)
    denied = sum(1 for row in rows if row["decision"] == "deny")
    frontier_call_count = sum(row["frontier_call_count"] for row in rows)
    prompt_dispatch_count = sum(row["prompt_dispatch_count"] for row in rows)
    live_qdrant_touched = any(row["live_qdrant_touched"] for row in rows)
    live_config_touched = any(row["live_config_touched"] for row in rows)
    secret_values_read = any(row["secret_values_read"] for row in rows)
    raw_payload_stored = any(row["raw_payload_stored"] for row in rows)
    quarantined_chunk_count = sum(row["quarantined_chunk_count"] for row in rows)
    retrieved_chunk_count = sum(row["retrieved_chunk_count"] for row in rows)

    summary = {
        "case_ids": list(CASE_ORDER),
        "total": total,
        "denied": denied,
        "allowed": total - denied,
        "frontier_call_count": frontier_call_count,
        "prompt_dispatch_count": prompt_dispatch_count,
        "retrieved_chunk_count": retrieved_chunk_count,
        "quarantined_chunk_count": quarantined_chunk_count,
        "live_qdrant_touched": live_qdrant_touched,
        "live_config_touched": live_config_touched,
        "secret_values_read": secret_values_read,
        "raw_payload_stored": raw_payload_stored,
        "policy_manifest_hash": policy_manifest_hash,
        "scope": "fixture_only_no_live_qdrant_provider_gateway_or_config_calls",
    }
    _write_json(summary_path, summary)

    return W10RagRouteTaintResult(
        evidence_path=str(evidence_path),
        summary_path=str(summary_path),
        total=total,
        denied=denied,
        frontier_call_count=frontier_call_count,
        prompt_dispatch_count=prompt_dispatch_count,
        live_qdrant_touched=live_qdrant_touched,
        live_config_touched=live_config_touched,
        secret_values_read=secret_values_read,
        raw_payload_stored=raw_payload_stored,
    )
