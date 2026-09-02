#!/usr/bin/env python3
"""Evaluate strict FTS5 versus opt-in NL session search on a safe corpus.

The corpus contains only synthetic, generic infrastructure text. Each case gets
its own temporary SQLite DB, so cases cannot contaminate one another or depend
on a developer's conversation history.

Usage:
  PYTHONPATH=. python scripts/nl_search_eval.py
  PYTHONPATH=. python scripts/nl_search_eval.py --json-out /tmp/nl-eval.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from hermes_state import SessionDB

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "hermes_state" / "fixtures" / "nl_search_eval_v1.json"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def evaluate_case(case: dict[str, str], natural_language: bool) -> dict[str, Any]:
    """Materialize one isolated target/distractor corpus and score its result."""
    with tempfile.TemporaryDirectory(prefix="hermes-nl-eval-") as tmp:
        db = SessionDB(db_path=Path(tmp) / "state.db")
        target_session = f"target-{case['id']}"
        db.create_session(target_session, source="eval")
        db.append_message(target_session, "assistant", case["target"])
        # Stable distractors make precision meaningful without adding private data.
        for index, text in enumerate((
            "generic scheduling record", "unrelated visual dashboard", "temporary document archive",
            "network inventory note", "ordinary release calendar",
        )):
            session_id = f"distractor-{case['id']}-{index}"
            db.create_session(session_id, source="eval")
            db.append_message(session_id, "assistant", text)
        started = time.perf_counter()
        rows = db.search_messages(case["query"], limit=5, natural_language=natural_language)
        elapsed_ms = (time.perf_counter() - started) * 1000
        db.close()

    ids = [row["session_id"] for row in rows]
    try:
        rank = ids.index(target_session) + 1
    except ValueError:
        rank = None
    return {
        "id": case["id"], "lang": case["lang"], "latency_ms": elapsed_ms,
        "returned": len(rows), "rank": rank,
        "hit1": rank == 1, "recall5": rank is not None,
        "precision5": (1.0 / len(rows)) if rank is not None and rows else 0.0,
        "rr": (1.0 / rank) if rank else 0.0,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, float | int]:
    count = len(results)
    return {
        "cases": count,
        "hit_at_1": sum(row["hit1"] for row in results) / count,
        "recall_at_5": sum(row["recall5"] for row in results) / count,
        "precision_at_5": sum(row["precision5"] for row in results) / count,
        "mrr": sum(row["rr"] for row in results) / count,
        "latency_p50_ms": percentile([row["latency_ms"] for row in results], 0.50),
        "latency_p95_ms": percentile([row["latency_ms"] for row in results], 0.95),
        "latency_mean_ms": statistics.fmean(row["latency_ms"] for row in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    modes = {
        "strict_fts5": [evaluate_case(case, False) for case in cases],
        "nl_opt_in": [evaluate_case(case, True) for case in cases],
    }
    report = {
        "corpus_version": corpus["version"],
        "corpus_cases": len(cases),
        "summary": {mode: summarize(rows) for mode, rows in modes.items()},
        "by_case": modes,
    }
    for mode, summary in report["summary"].items():
        print(
            f"{mode}: cases={summary['cases']} hit@1={summary['hit_at_1']:.3f} "
            f"recall@5={summary['recall_at_5']:.3f} precision@5={summary['precision_at_5']:.3f} "
            f"MRR={summary['mrr']:.3f} p50={summary['latency_p50_ms']:.1f}ms "
            f"p95={summary['latency_p95_ms']:.1f}ms"
        )
    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
