from __future__ import annotations

import json
from pathlib import Path

from scripts.measure_abstention_gates import measure_payload


RAW = Path(r"C:/Users/downl/AppData/Local/Temp/nsfw-bge-m3-v5-quantisation-query-results.json")


def test_measurement_reports_missing_scores_without_inventing_gate_metrics() -> None:
    payload = json.loads(RAW.read_text(encoding="utf-8"))
    report = measure_payload(payload)

    assert report["measurement_status"] == "baseline_only_missing_score_fields"
    assert report["candidate_count"] == 4
    assert report["query_count"] == 90
    assert report["negative_query_count"] == 10
    assert report["score_fields_available"] == []
    assert report["query_rows"][0]["query"] == "frontend.language"
    assert report["query_rows"][-1]["group"] == "negative"
    assert report["production_path_changed"] is False
    assert report["gates"]["dense_similarity_floor"]["status"] == "not_measurable"
    assert report["gates"]["top_score_margin"]["status"] == "not_measurable"
    assert report["gates"]["lexical_dense_agreement"]["status"] == "not_measurable"


def test_measurement_preserves_authoritative_baseline_metrics() -> None:
    payload = json.loads(RAW.read_text(encoding="utf-8"))
    report = measure_payload(payload)

    q6 = report["candidates"]["Q6_K"]
    assert q6["negative_false_recall_rate"] == 1.0
    assert q6["negative_no_result_precision"] == 0.0
    assert q6["overall_recall_at_8"] == 0.6666666666666666
    assert q6["state_mutation_count"] == 0


def test_measurement_rejects_non_matching_query_sets() -> None:
    payload = json.loads(RAW.read_text(encoding="utf-8"))
    payload[1]["benchmark"]["variants"]["B_hybrid_bge_m3"]["query_results"].pop()

    report = measure_payload(payload)

    assert report["measurement_status"] == "invalid_input_query_alignment"
    assert report["alignment_errors"]


def test_measurement_accepts_candidate_score_schema_without_sweeping() -> None:
    payload = json.loads(RAW.read_text(encoding="utf-8"))
    row = payload[0]["benchmark"]["variants"]["B_hybrid_bge_m3"]["query_results"][0]
    row["candidates"] = [{
        "node_id": "frontend.language",
        "lexical_rank": 1,
        "dense_rank": 2,
        "dense_similarity": 0.8,
        "rrf_score": 0.03,
        "source_count": 2,
        "best_rank": 1,
        "final_rank": 1,
        "selected_into_top8": True,
    }]
    row["observation"] = {
        "top1_dense_similarity": 0.8,
        "top2_dense_similarity": 0.4,
        "dense_top_margin": 0.4,
        "top1_rrf_score": 0.03,
        "top2_rrf_score": 0.02,
        "rrf_top_margin": 0.01,
        "lexical_dense_top1_agreement": True,
        "lexical_dense_expected_overlap": 1,
    }

    report = measure_payload(payload)

    assert report["measurement_status"] == "scores_available"
    assert report["score_fields_available"] == ["dense_rank", "dense_similarity", "lexical_rank", "rrf_score"]
    assert report["query_rows"][0]["dense_top_margin"] == 0.4
    assert report["gates"]["dense_similarity_floor"]["status"] == "ready_for_sweep"


__all__ = ["RAW"]
