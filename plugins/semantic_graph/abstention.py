"""Small deterministic retrieval abstention gate."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


DENSE_FLOOR = 0.35
DENSE_STRONG_FLOOR = 0.50
DENSE_MARGIN_FLOOR = 0.10
RRF_MARGIN_FLOOR = 0.01
SOURCE_COUNT_FLOOR = 2
RETENTION_FLOOR = 0.10


def extract_retrieval_features(
    candidates: Sequence[Mapping[str, Any]],
    *,
    query_length: int,
) -> dict[str, Any]:
    """Extract the fixed Phase 5 observation vector without query content."""
    dense = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.get("dense_rank") is not None
            and candidate.get("dense_similarity") is not None
        ),
        key=lambda candidate: (
            int(candidate["dense_rank"]),
            str(candidate.get("node_id") or ""),
        ),
    )
    rrf = sorted(
        candidates,
        key=lambda candidate: (
            -float(candidate.get("rrf_score") or 0.0),
            str(candidate.get("node_id") or ""),
        ),
    )
    top1_dense = float(dense[0]["dense_similarity"]) if dense else None
    top2_dense = float(dense[1]["dense_similarity"]) if len(dense) > 1 else None
    top1_rrf = float(rrf[0].get("rrf_score") or 0.0) if rrf else None
    top2_rrf = (
        float(rrf[1].get("rrf_score") or 0.0) if len(rrf) > 1 else None
    )
    lexical_top = next(
        (
            str(candidate.get("node_id") or "")
            for candidate in candidates
            if candidate.get("lexical_rank") == 1
        ),
        None,
    )
    dense_top = str(dense[0].get("node_id") or "") if dense else None
    top = rrf[0] if rrf else None
    top_shadow = dict(top.get("cognitive_shadow") or {}) if top else {}
    linked_shadows = [
        dict(candidate.get("cognitive_shadow") or {})
        for candidate in candidates
        if (candidate.get("cognitive_shadow") or {}).get("belief_status")
    ]
    linked_count = len(linked_shadows)
    current_count = sum(
        str(shadow.get("belief_status") or "").strip().lower()
        in {"current", "context_dependent"}
        for shadow in linked_shadows
    )
    latent_count = sum(
        str(shadow.get("access_state") or "").strip().lower() == "latent"
        for shadow in linked_shadows
    )
    noncurrent_count = sum(
        str(shadow.get("belief_status") or "").strip().lower()
        in {"contested", "superseded", "retracted"}
        for shadow in linked_shadows
    )
    return {
        "top1_dense_similarity": top1_dense,
        "top2_dense_similarity": top2_dense,
        "dense_margin": (
            top1_dense - top2_dense
            if top1_dense is not None and top2_dense is not None
            else None
        ),
        "top1_rrf_score": top1_rrf,
        "rrf_margin": (
            top1_rrf - top2_rrf
            if top1_rrf is not None and top2_rrf is not None
            else None
        ),
        "lexical_dense_top1_agreement": bool(
            lexical_top and dense_top and lexical_top == dense_top
        ),
        "source_count": int(top.get("source_count") or 0) if top else 0,
        "candidate_count": len(candidates),
        "projected_retention": top_shadow.get("projected_retention"),
        "current_ratio": current_count / linked_count if linked_count else 0.0,
        "latent_ratio": latent_count / linked_count if linked_count else 0.0,
        "noncurrent_ratio": (
            noncurrent_count / linked_count if linked_count else 0.0
        ),
        "query_length": max(0, int(query_length)),
    }


def decide_abstention(features: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a bounded boolean gate; missing dense evidence fails open."""
    dense = features.get("top1_dense_similarity")
    if dense is None:
        return {
            "abstain": False,
            "reason": "dense_unavailable_fail_open",
        }
    dense_margin = features.get("dense_margin")
    rrf_margin = features.get("rrf_margin")
    retention = features.get("projected_retention")
    dense_value = float(dense)
    agreement = bool(features.get("lexical_dense_top1_agreement"))
    source_ok = (
        int(features.get("source_count") or 0) >= SOURCE_COUNT_FLOOR
    )
    dense_margin_ok = (
        dense_margin is not None
        and float(dense_margin) >= DENSE_MARGIN_FLOOR
    )
    rrf_margin_ok = (
        rrf_margin is not None and float(rrf_margin) >= RRF_MARGIN_FLOOR
    )
    state_ok = (
        retention is None or float(retention) >= RETENTION_FLOOR
    ) and float(features.get("noncurrent_ratio") or 0.0) < 1.0
    evidence_ok = (
        dense_value >= DENSE_STRONG_FLOOR
        or (
            dense_value >= DENSE_FLOOR
            and (agreement or dense_margin_ok)
        )
        or rrf_margin_ok
    )
    passed = source_ok and state_ok and evidence_ok
    return {
        "abstain": not passed,
        "reason": "passed" if passed else "weak_evidence",
    }


__all__ = [
    "DENSE_FLOOR",
    "DENSE_STRONG_FLOOR",
    "DENSE_MARGIN_FLOOR",
    "RRF_MARGIN_FLOOR",
    "SOURCE_COUNT_FLOOR",
    "RETENTION_FLOOR",
    "decide_abstention",
    "extract_retrieval_features",
]
