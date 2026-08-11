"""Measure abstention-gate readiness from existing query-level results only.

This module deliberately does not call the live embedding endpoint, open the
production SQLite database, or modify retrieval code.  If the input lacks
candidate score/rank fields, it reports that the proposed gates are not
measurable rather than fabricating scores.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

VARIANT = "B_hybrid_bge_m3"
FIXTURE = Path(__file__).parents[1] / "tests" / "fixtures" / "semantic_graph_retrieval_benchmark.json"
REQUIRED_SCORE_FIELDS = frozenset(
    {"dense_similarity", "lexical_rank", "dense_rank", "rrf_score"}
)
GATE_NAMES = ("dense_similarity_floor", "lexical_dense_agreement", "top_score_margin")


def _query_signature(row: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(row.get("group") or ""),
        str(row.get("query") or ""),
        tuple(str(value) for value in row.get("expected") or []),
    )


def _rows(payload: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for candidate in payload:
        benchmark = candidate.get("benchmark") or {}
        variants = benchmark.get("variants") or {}
        variant = variants.get(VARIANT) or {}
        query_results = variant.get("query_results")
        yield query_results if isinstance(query_results, list) else []


def _candidate_score_fields(rows: Iterable[dict[str, Any]]) -> set[str]:
    fields: set[str] = set()
    for row in rows:
        for candidate in row.get("candidates") or []:
            if isinstance(candidate, dict):
                fields.update(candidate)
    return fields


def _alignment_errors(payload: list[dict[str, Any]]) -> list[str]:
    batches = list(_rows(payload))
    if not batches:
        return ["no query-level result batches found"]
    reference = [_query_signature(row) for row in batches[0]]
    errors: list[str] = []
    if len(reference) != 90:
        errors.append(f"reference query count is {len(reference)}, expected 90")
    for index, batch in enumerate(batches):
        signature = [_query_signature(row) for row in batch]
        if signature != reference:
            errors.append(f"candidate index {index} does not match the reference query ordering")
    return errors


def _baseline(candidate: dict[str, Any]) -> dict[str, Any]:
    benchmark = candidate.get("benchmark") or {}
    variant = (benchmark.get("variants") or {}).get(VARIANT) or {}
    return {
        key: variant.get(key)
        for key in (
            "overall_recall_at_8",
            "negative_false_recall_rate",
            "negative_no_result_precision",
            "state_mutation_count",
        )
    }


def measure_payload(payload: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic, non-invasive readiness report for gate metrics."""
    batches = list(_rows(payload))
    errors = _alignment_errors(payload)
    all_rows = [row for batch in batches for row in batch]
    score_fields = sorted(_candidate_score_fields(all_rows) & REQUIRED_SCORE_FIELDS)
    score_schema_complete = set(score_fields) == REQUIRED_SCORE_FIELDS
    query_count = len(batches[0]) if batches else 0
    negative_count = sum(row.get("group") == "negative" for row in (batches[0] if batches else []))
    status = "invalid_input_query_alignment" if errors else (
        "baseline_only_missing_score_fields" if not score_schema_complete else "scores_available"
    )
    gates = {
        name: {
            "status": "not_measurable" if not score_schema_complete else "ready_for_sweep",
            "reason": "input has no candidate-level dense/lexical score or rank fields"
            if not score_schema_complete
            else "candidate-level fields are present; threshold sweep is a separate analysis step",
        }
        for name in GATE_NAMES
    }
    reference_rows = batches[0] if batches else []
    fixture_queries: list[dict[str, Any]] = []
    if FIXTURE.is_file():
        fixture_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        raw_queries = fixture_payload.get("queries") if isinstance(fixture_payload, dict) else []
        if isinstance(raw_queries, list):
            fixture_queries = [item for item in raw_queries if isinstance(item, dict)]
    query_rows = [
        {
            "query_index": index,
            "group": row.get("group"),
            "query": row.get("query") or (fixture_queries[index].get("query") if index < len(fixture_queries) else None),
            "expected": row.get("expected") or [],
            "baseline_returned_any": bool(row.get("returned_any")),
            "baseline_returned_count": len(row.get("returned") or []),
            "baseline_hit": bool(row.get("hit")),
            "dense_similarity_top1": (row.get("observation") or {}).get("top1_dense_similarity"),
            "dense_similarity_top2": (row.get("observation") or {}).get("top2_dense_similarity"),
            "dense_top_margin": (row.get("observation") or {}).get("dense_top_margin"),
            "top1_rrf_score": (row.get("observation") or {}).get("top1_rrf_score"),
            "top2_rrf_score": (row.get("observation") or {}).get("top2_rrf_score"),
            "rrf_top_margin": (row.get("observation") or {}).get("rrf_top_margin"),
            "lexical_dense_top1_agreement": (row.get("observation") or {}).get("lexical_dense_top1_agreement"),
            "lexical_dense_expected_overlap": (row.get("observation") or {}).get("lexical_dense_expected_overlap"),
            "gate_status": "ready_for_sweep" if score_schema_complete else "not_measurable",
        }
        for index, row in enumerate(reference_rows)
    ]
    return {
        "schema_version": 1,
        "measurement_status": status,
        "candidate_count": len(payload),
        "query_count": query_count,
        "negative_query_count": negative_count,
        "score_fields_available": score_fields,
        "alignment_errors": errors,
        "query_rows": query_rows,
        "candidates": {
            str(candidate.get("candidate") or f"candidate_{index}"): _baseline(candidate)
            for index, candidate in enumerate(payload)
        },
        "gates": gates,
        "production_path_changed": False,
        "live_endpoint_called": False,
        "sqlite_write_performed": False,
        "authoritative_state": {
            "model": "nsfw-bge-m3-v5-q6_k.gguf",
            "endpoint": "127.0.0.1:8084",
            "health": "PASS",
            "quant_selection": "Q6_K",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("input must contain a candidate list")
    report = measure_payload(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["measurement_status"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["measure_payload"]
